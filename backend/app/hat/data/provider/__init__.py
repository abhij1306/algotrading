"""The provider data socket: the one Fyers WebSocket HAT owns (HAT-OVR-006, K3).

`data_socket` is the seam to the SDK socket, `provider_session` its connect, replay and reconnect
lifecycle, `fyers_websocket` the only module that builds the SDK socket, `ticks` the pure tick
transform and `metrics` the cut-down counters. Only `app.hat.data.feed` builds a session.
"""
