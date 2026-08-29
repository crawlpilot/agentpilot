"""The control plane: platform-owned state and policy injected into the browser layer.

Everything here answers a question that is about *how agentpilot is deployed*
rather than about driving a browser -- shared cross-process state, tenant
mapping, site knowledge. The browser layer defines the seams
(`agentpilot.policy`) and ships inert single-process defaults; this package
supplies the real implementations, and `gateway.wiring` injects them.

This is the landing zone the browserpilot extraction needs: after the split
(Phase 7) `agentpilot.control` stays behind with the platform while the browser
layer ships without it -- and without Redis.
"""
