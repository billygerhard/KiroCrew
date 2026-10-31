---
title: Tailnet-verified phone sessions get revoked by refresh-token reuse detection while in active use
labels: bug
---

## What

A phone signed in through the tailnet QR with `dashboard.qr_session_persist_across_restart`
on gets a 30-day, `require_peer` refresh chain. The session is meant to last until it
goes unused. In practice an iOS home-screen web app gets signed out at random while
it is being used.

The cause is reuse detection in `dashboard/handlers/auth_refresh.py`. Each refresh
rotates the refresh cookie and marks the old `jti` consumed. If a consumed `jti` comes
back after `REFRESH_GRACE_SECS` (60 s), or from a different client IP, it is treated as
theft and the whole chain is revoked (`state.revoke_chain`).

Seen on 2026-10-31 in `security_events.jsonl`:

- `12:24:22Z refresh_token_use reuse_detected <chain>`: the chain was revoked.
- `15:08:12Z refresh_token_use chain_revoked`: a later refresh on the same chain was
  refused, and the user was signed out.
- Several `refresh_token_use no_cookie` events through the afternoon, each a refresh
  call that arrived with no refresh cookie at all.

The events don't record the device, so it isn't proven which client replayed the
token. The likely sequence is ordinary iOS behaviour for standalone web apps. iOS
suspends or kills the app's web process when the user switches away. If that happens
after the server has rotated the cookie but before the new `Set-Cookie` is stored, the
app's cookie jar still holds the old refresh token. The next refresh, minutes later,
presents a consumed `jti` outside the 60-second window, and the chain is revoked.

## Why

For a `require_peer` chain, the theft model behind reuse detection mostly doesn't
apply. By the time the reuse branch runs, the handler has already verified the
caller's tailnet identity against the chain's pinned peer: that check is deliberately
placed before reuse detection. A replay that passes it comes from the device the
session belongs to. The code comment says "a thief who CAN verify still trips reuse
detection normally", but on a tailnet the realistic caller who can verify is that same
device recovering from a lost response.

The grace window's same-IP rule also adds nothing here. Behind `tailscale serve`
every request reaches the gateway from 127.0.0.1, as the comments in
`dashboard/handlers/tailnet_mobile.py` already point out.

The result is that the session shape built to survive restarts and inactivity is the
one most likely to be killed during normal use, and the user has to re-scan the QR
code.

## Suggested fix

- For chains with `require_peer`, once the verified peer matches the pinned peer, let
  a replay of the chain head's predecessor (the single most recently consumed `jti`,
  as `grace_replacement` already keeps) re-serve the cached replacement pair for much
  longer than 60 s. The lifetime of the issued access token is a reasonable bound.
- Keep revoking for any older consumed `jti`, for unverified or mismatched peers, and
  for chains without `require_peer`, so the existing theft signal is unchanged
  everywhere else.
- Audit the extended replay with its own outcome (for example
  `verified_peer_replay`), so it stays visible in the security log.
