------------------------------ MODULE RegimeConfirmation ------------------------------
(***************************************************************************)
(* The 3-day confirmation of the regime state (HAT-PORT-030).       *)
(*                                                                         *)
(* Each trading day the regime job reads one raw state (the classifier's   *)
(* output) and updates the confirmed state: the confirmed state becomes    *)
(* the raw state only on the 3rd consecutive day the raw state has been    *)
(* that value. The first reading ever is the confirmed state (bootstrap).  *)
(*                                                                         *)
(* Bounds: MaxDays = 7 daily readings (the longest sequence explored: it   *)
(*   holds every pattern of one confirmation and one reversal); 4 raw      *)
(*   states.                                                               *)
(* Assumptions: each daily reading is one database transaction (the        *)
(*   `regime` job body; `hat_regime_state` is unique per trade date, so a  *)
(*   rerun adds no reading); the raw state of a day is arbitrary (the      *)
(*   classifier is covered by PBT regime.classification_total); no day is  *)
(*   skipped (a missed day fails the job and the cycle stops).             *)
(*                                                                         *)
(* Properties: ThreeDayRule and ConfirmsAfterThree (invariants over the    *)
(*   raw history, which the model keeps only to state them).               *)
(***************************************************************************)
EXTENDS Naturals, Sequences

CONSTANTS MaxDays

States == {"bull_quiet", "bull_volatile", "correction", "bear"}

VARIABLES
    hist,           \* the raw states read so far, oldest first
    confirmed,      \* the confirmed state ("none" before the first reading)
    run,            \* the model's running count of consecutive equal raw readings
    prevConfirmed   \* the confirmed state before the latest reading

vars == <<hist, confirmed, run, prevConfirmed>>

Init ==
    /\ hist = <<>>
    /\ confirmed = "none"
    /\ run = 0
    /\ prevConfirmed = "none"

Need == 3

\* The confirmed state after a reading of r that makes the run newRun.
Next3(r, newRun) ==
    IF r # confirmed /\ newRun >= Need THEN r ELSE confirmed

\* code: backend/app/hat/engine/portfolio/regime.py:confirm_step  (the first reading: no
\* previous confirmation, so the raw state is the confirmed state)
Bootstrap(r) ==
    /\ hist = <<>>
    /\ hist' = <<r>>
    /\ confirmed' = r
    /\ run' = 1
    /\ prevConfirmed' = r

\* code: backend/app/hat/engine/portfolio/regime.py:confirm_step  (the raw state equals the
\* latest one: the run grows)
Repeat(r) ==
    /\ Len(hist) \in 1..(MaxDays - 1)
    /\ r = hist[Len(hist)]
    /\ hist' = Append(hist, r)
    /\ confirmed' = Next3(r, run + 1)
    /\ run' = run + 1
    /\ prevConfirmed' = confirmed

\* code: backend/app/hat/engine/portfolio/regime.py:confirm_step  (the raw state differs from
\* the latest one: the run restarts at 1)
Switch(r) ==
    /\ Len(hist) \in 1..(MaxDays - 1)
    /\ r # hist[Len(hist)]
    /\ hist' = Append(hist, r)
    /\ confirmed' = Next3(r, 1)
    /\ run' = 1
    /\ prevConfirmed' = confirmed

Next ==
    \E r \in States : Bootstrap(r) \/ Repeat(r) \/ Switch(r)

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ hist \in Seq(States)
    /\ Len(hist) <= MaxDays
    /\ confirmed \in States \cup {"none"}
    /\ run \in 0..(MaxDays + 1)

\* A change of the confirmed state happens only when the raw state was that new state on the
\* last three days (the 3rd consecutive day), read from the raw history, not from `run`.
ThreeDayRule ==
    (Len(hist) >= 2 /\ confirmed # prevConfirmed) =>
        /\ Len(hist) >= 3
        /\ hist[Len(hist)] = confirmed
        /\ hist[Len(hist) - 1] = confirmed
        /\ hist[Len(hist) - 2] = confirmed

\* Whenever the raw state has stayed at s for 3 consecutive days, the confirmed state is s at
\* the end of the third day (and after).
ConfirmsAfterThree ==
    (Len(hist) >= 3
        /\ hist[Len(hist)] = hist[Len(hist) - 1]
        /\ hist[Len(hist)] = hist[Len(hist) - 2])
    => confirmed = hist[Len(hist)]
==============================================================================
