"""
Integration tests for startup sequence validation

Tests Property 2: Startup Sequence Integrity
Validates: Requirements 6.4

These tests verify that the application startup sequence executes in the correct
order and that failures are properly handled.
"""

import asyncio

import pytest

from app.services.startup_validator import StartupSequence, StartupStep


class TestStartupSequenceIntegrity:
    """Test suite for startup sequence validation"""

    @pytest.mark.asyncio
    async def test_startup_steps_execute_in_order(self):
        """
        Test that startup steps execute in the correct order.

        Property: For any application startup, steps should execute in the
        defined order and be tracked correctly.
        """
        sequence = StartupSequence()
        execution_order = []

        async def step1():
            execution_order.append("step1")
            await asyncio.sleep(0)

        async def step2():
            execution_order.append("step2")
            await asyncio.sleep(0)

        async def step3():
            execution_order.append("step3")
            await asyncio.sleep(0)

        # Execute steps in order
        await sequence.execute_step(StartupStep.SET_EVENT_LOOP, step1, required=True)
        await sequence.execute_step(StartupStep.VALIDATE_SYMBOL_MASTER, step2, required=True)
        await sequence.execute_step(StartupStep.VALIDATE_DATABASE, step3, required=True)

        # Verify execution order
        assert execution_order == ["step1", "step2", "step3"]
        assert len(sequence.completed_steps) == 3
        assert sequence.completed_steps[0] == StartupStep.SET_EVENT_LOOP
        assert sequence.completed_steps[1] == StartupStep.VALIDATE_SYMBOL_MASTER
        assert sequence.completed_steps[2] == StartupStep.VALIDATE_DATABASE

    @pytest.mark.asyncio
    async def test_required_step_failure_stops_sequence(self):
        """
        Test that a required step failure prevents subsequent steps from executing.

        Property: For any required step failure, all subsequent steps should be
        prevented from executing.
        """
        sequence = StartupSequence()
        execution_log = []

        async def step1():
            execution_log.append("step1_executed")
            await asyncio.sleep(0)

        async def step2_fails():
            execution_log.append("step2_attempted")
            await asyncio.sleep(0)
            raise ValueError("Step 2 failed")

        async def step3():
            execution_log.append("step3_executed")
            await asyncio.sleep(0)

        # Execute step 1 successfully
        result1 = await sequence.execute_step(
            StartupStep.SET_EVENT_LOOP,
            step1,
            required=True
        )
        assert result1 is True

        # Execute step 2 which fails
        with pytest.raises(ValueError, match="Step 2 failed"):
            await sequence.execute_step(
                StartupStep.VALIDATE_SYMBOL_MASTER,
                step2_fails,
                required=True
            )

        # Execute step 3 - should be skipped
        result3 = await sequence.execute_step(
            StartupStep.VALIDATE_DATABASE,
            step3,
            required=True
        )
        assert result3 is False

        # Verify execution log
        assert "step1_executed" in execution_log
        assert "step2_attempted" in execution_log
        assert "step3_executed" not in execution_log

        # Verify sequence state
        assert len(sequence.completed_steps) == 1
        assert sequence.failed_step == StartupStep.VALIDATE_SYMBOL_MASTER
        assert not sequence.is_complete()

    @pytest.mark.asyncio
    async def test_optional_step_failure_continues_sequence(self):
        """
        Test that an optional step failure allows subsequent steps to execute.

        Property: For any optional step failure, subsequent steps should
        continue executing normally.
        """
        sequence = StartupSequence()
        execution_log = []

        async def step1():
            execution_log.append("step1")
            await asyncio.sleep(0)

        async def step2_fails():
            execution_log.append("step2_attempted")
            await asyncio.sleep(0)
            raise ValueError("Optional step failed")

        async def step3():
            execution_log.append("step3")
            await asyncio.sleep(0)

        # Execute step 1
        await sequence.execute_step(
            StartupStep.SET_EVENT_LOOP,
            step1,
            required=True
        )

        # Execute step 2 (optional) which fails
        result2 = await sequence.execute_step(
            StartupStep.VALIDATE_FYERS_TOKEN,
            step2_fails,
            required=False  # Optional step
        )
        assert result2 is False

        # Execute step 3 - should still execute
        result3 = await sequence.execute_step(
            StartupStep.VALIDATE_DATABASE,
            step3,
            required=True
        )
        assert result3 is True

        # Verify all steps were attempted
        assert "step1" in execution_log
        assert "step2_attempted" in execution_log
        assert "step3" in execution_log

        # Verify sequence state
        assert len(sequence.completed_steps) == 2  # step1 and step3
        assert sequence.is_complete()  # No required failures

    @pytest.mark.asyncio
    async def test_failure_logging_with_context(self):
        """
        Test that failures are logged with full context.

        Property: For any step failure, the system should log the failure
        with the step name and error details.
        """
        sequence = StartupSequence()

        async def failing_step():
            raise RuntimeError("Database connection failed: timeout after 30s")

        # Execute failing step
        with pytest.raises(RuntimeError):
            await sequence.execute_step(
                StartupStep.VALIDATE_DATABASE,
                failing_step,
                required=True
            )

        # Verify failure was tracked
        assert sequence.failed_step == StartupStep.VALIDATE_DATABASE
        assert not sequence.is_complete()

    @pytest.mark.asyncio
    async def test_get_status_returns_correct_state(self):
        """
        Test that get_status() returns accurate sequence state.

        Property: The status should accurately reflect completed steps,
        failed steps, and overall completion state.
        """
        sequence = StartupSequence()

        async def step1():
            # Async no-op step to emulate startup callback execution.
            await asyncio.sleep(0)

        async def step2():
            # Async no-op step to emulate startup callback execution.
            await asyncio.sleep(0)

        # Initial status
        status = sequence.get_status()
        assert status["completed_steps"] == []
        assert status["failed_step"] is None
        assert status["is_complete"] is True
        assert status["total_completed"] == 0

        # After first step
        await sequence.execute_step(StartupStep.SET_EVENT_LOOP, step1, required=True)
        status = sequence.get_status()
        assert len(status["completed_steps"]) == 1
        assert status["completed_steps"][0] == "set_event_loop"
        assert status["total_completed"] == 1

        # After second step
        await sequence.execute_step(StartupStep.VALIDATE_SYMBOL_MASTER, step2, required=True)
        status = sequence.get_status()
        assert len(status["completed_steps"]) == 2
        assert status["total_completed"] == 2
        assert status["is_complete"] is True

    @pytest.mark.asyncio
    async def test_multiple_failures_track_first_failure(self):
        """
        Test that multiple failures track the first failure point.

        Property: When multiple steps fail, the sequence should track
        the first failure point.
        """
        sequence = StartupSequence()

        async def step1():
            # Async no-op step to emulate startup callback execution.
            await asyncio.sleep(0)

        async def step2_fails():
            raise ValueError("First failure")

        async def step3_fails():
            raise ValueError("Second failure")

        # Execute successful step
        await sequence.execute_step(StartupStep.SET_EVENT_LOOP, step1, required=True)

        # Execute first failing step
        with pytest.raises(ValueError, match="First failure"):
            await sequence.execute_step(
                StartupStep.VALIDATE_SYMBOL_MASTER,
                step2_fails,
                required=True
            )

        # Try to execute second failing step (should be skipped)
        result = await sequence.execute_step(
            StartupStep.VALIDATE_DATABASE,
            step3_fails,
            required=True
        )

        # Verify first failure is tracked
        assert sequence.failed_step == StartupStep.VALIDATE_SYMBOL_MASTER
        assert result is False

    @pytest.mark.asyncio
    async def test_all_startup_steps_defined(self):
        """
        Test that all required startup steps are defined in the enum.

        Property: The StartupStep enum should contain all steps from the
        startup sequence specification.
        """
        required_steps = [
            "SET_EVENT_LOOP",
            "VALIDATE_SYMBOL_MASTER",
            "VALIDATE_DATABASE",
            "VALIDATE_FYERS_TOKEN",
            "LOAD_INDEX_UNIVERSE",
            "CONNECT_LIVE_MARKET"
        ]

        enum_steps = [step.name for step in StartupStep]

        for step in required_steps:
            assert step in enum_steps, f"Missing required step: {step}"

    @pytest.mark.asyncio
    async def test_log_summary_reflects_state(self):
        """
        Test that log_summary() accurately reflects the sequence state.

        Property: The summary should indicate success or failure with
        appropriate details.
        """
        sequence = StartupSequence()

        async def step1():
            # Async no-op step to emulate startup callback execution.
            await asyncio.sleep(0)

        async def step2_fails():
            raise ValueError("Test failure")

        # Successful sequence
        await sequence.execute_step(StartupStep.SET_EVENT_LOOP, step1, required=True)
        sequence.log_summary()  # Should log success
        assert sequence.is_complete()

        # Failed sequence
        sequence2 = StartupSequence()
        await sequence2.execute_step(StartupStep.SET_EVENT_LOOP, step1, required=True)

        with pytest.raises(ValueError):
            await sequence2.execute_step(
                StartupStep.VALIDATE_SYMBOL_MASTER,
                step2_fails,
                required=True
            )

        sequence2.log_summary()  # Should log failure
        assert not sequence2.is_complete()
        assert sequence2.failed_step == StartupStep.VALIDATE_SYMBOL_MASTER


# Property-Based Test Marker
pytestmark = pytest.mark.integration
