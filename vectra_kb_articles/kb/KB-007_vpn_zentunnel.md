---
doc_id: KB-007
title: Connecting to VPN with ZenTunnel
status: current
effective_date: 2026-04-01
supersedes: KB-002
owner: Network Ops
---
# Connecting to VPN with ZenTunnel

ZenTunnel replaced SecureLink for all staff on 1 April 2026. SecureLink stops working on 30 June 2026.

## Install
1. Open Company Portal and install **ZenTunnel Agent 3.x**. Supported on **Windows 11 and macOS 14+ only**.
2. Gateway: **zt-gw.vectralogistics.example** (selected automatically after sign-in).
3. Sign in with single sign-on (SSO) and approve the MFA prompt.

## Common errors
| Error | Meaning | Fix |
| --- | --- | --- |
| ZT-401 | SSO token expired or device not enrolled | Sign out of ZenTunnel, sign in again; if it persists, check the device is enrolled in Company Portal |
| ZT-503 | Gateway unreachable | Check your home internet; switch from 5 GHz to a mobile hotspot to test; if the gateway is down for everyone this is a P1 |
| Stuck on "Negotiating" | UDP 51820 blocked by the local network | Toggle "Use TCP fallback" in ZenTunnel settings |
| Connected but WMS will not open | Split tunnel profile outdated | Click "Refresh profile" in ZenTunnel, then restart the browser |

Unsupported operating systems (including Linux) are not covered by this guide; raise a ticket for Network Ops to assess.
