const $ = (id) => document.getElementById(id);

const log = $("log");
function logLine(msg) {
  const ts = new Date().toISOString().split("T")[1].replace("Z", "");
  log.textContent += `[${ts}] ${msg}\n`;
  log.scrollTop = log.scrollHeight;
}

// --- gateway health ---

async function checkGateway() {
  const dot = $("gateway-dot");
  const label = $("gateway-status");
  try {
    const res = await fetch("/healthz");
    if (res.ok) {
      dot.classList.add("ok");
      label.textContent = "gateway ok";
      return;
    }
    throw new Error(`status ${res.status}`);
  } catch (err) {
    dot.classList.remove("ok");
    label.textContent = "gateway unreachable";
  }
}
checkGateway();
setInterval(checkGateway, 10_000);

// --- session state ---

let session = null; // { session_id, node_id, tier_used }

function setSessionUI(active) {
  $("release-btn").disabled = !active;
  $("open-btn").disabled = active;
  $("nav-btn").disabled = !active;
  $("snapshot-btn").disabled = !active;
  $("extract-btn").disabled = !active;
  $("screenshot-btn").disabled = !active;
  $("connect-btn").disabled = !active;
  $("session-info").hidden = !active;
}

async function apiCall(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* no body */
  }
  if (!res.ok) {
    const detail = body ? body.error || JSON.stringify(body) : res.statusText;
    const retryAfter = res.headers.get("Retry-After");
    throw new Error(
      `${res.status} ${body ? body.code || "" : ""}: ${detail}` +
        (retryAfter ? ` (retry after ${retryAfter}s)` : "")
    );
  }
  return body;
}

$("open-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  const payload = {
    tenant: form.get("tenant"),
    domain: form.get("domain"),
    name: form.get("name"),
    tier: form.get("tier"),
    headful: form.has("headful"),
    block_popups: form.has("block_popups"),
    live_view: form.has("live_view"),
  };
  logLine(`opening session ${payload.tenant}/${payload.domain}/${payload.name} (tier=${payload.tier})`);
  try {
    const resp = await apiCall("/v1/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    session = resp;
    $("session-id").textContent = resp.session_id;
    $("session-node").textContent = resp.metadata.node_id;
    $("session-tier").textContent = resp.metadata.tier_used;
    setSessionUI(true);
    logLine(`session open: ${resp.session_id} (${resp.metadata.duration_ms.toFixed(0)}ms)`);
  } catch (err) {
    logLine(`open failed: ${err.message}`);
  }
});

$("release-btn").addEventListener("click", async () => {
  if (!session) return;
  disconnectLiveView();
  try {
    await apiCall(`/v1/sessions/${session.session_id}`, { method: "DELETE" });
    logLine(`released ${session.session_id}`);
  } catch (err) {
    logLine(`release failed: ${err.message}`);
  }
  session = null;
  setSessionUI(false);
});

async function execute(actions) {
  if (!session) return null;
  return apiCall(`/v1/sessions/${session.session_id}/execute`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ actions }),
  });
}

$("nav-btn").addEventListener("click", async () => {
  const url = $("nav-url").value.trim();
  if (!url) return;
  logLine(`navigate -> ${url}`);
  try {
    await execute([{ type: "navigate", url }]);
    logLine("navigate ok");
  } catch (err) {
    logLine(`navigate failed: ${err.message}`);
  }
});

function printSnapshotNode(node, depth, lines) {
  const ref = node.ref ? ` [ref=${node.ref}]` : "";
  const name = node.name ? ` "${node.name}"` : "";
  lines.push(`${"  ".repeat(depth)}- ${node.role}${name}${ref}`);
  for (const child of node.children) printSnapshotNode(child, depth + 1, lines);
}

$("snapshot-btn").addEventListener("click", async () => {
  logLine("snapshot...");
  try {
    const result = await execute([{ type: "snapshot" }]);
    const lines = [];
    printSnapshotNode(result.snapshots[0].root, 0, lines);
    logLine(`snapshot epoch=${result.snapshots[0].epoch}\n${lines.join("\n")}`);
  } catch (err) {
    logLine(`snapshot failed: ${err.message}`);
  }
});

$("extract-btn").addEventListener("click", async () => {
  logLine("extract...");
  try {
    const result = await execute([{ type: "extract", format: "markdown" }]);
    const text = result.extracts[0] || "";
    logLine(`extract (${text.length} chars):\n${text.slice(0, 600)}`);
  } catch (err) {
    logLine(`extract failed: ${err.message}`);
  }
});

$("screenshot-btn").addEventListener("click", async () => {
  logLine("screenshot...");
  try {
    const result = await execute([{ type: "screenshot" }]);
    const bytes = atob(result.screenshots[0] || "").length;
    logLine(`screenshot: ${bytes} bytes (base64 in response, not rendered in log)`);
  } catch (err) {
    logLine(`screenshot failed: ${err.message}`);
  }
});

// --- live view ---

let ws = null;
let mode = "view";
let lastFrameUrl = null;

$("mode-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn || ws) return; // require reconnect to change mode
  document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  mode = btn.dataset.mode;
});

function connectLiveView() {
  if (!session || ws) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const url = `${proto}://${location.host}/v1/sessions/${session.session_id}/live-view?mode=${mode}`;
  ws = new WebSocket(url);
  ws.binaryType = "blob";

  ws.onopen = () => {
    logLine(`live-view connected (mode=${mode})`);
    $("connect-btn").textContent = "Disconnect";
    $("live-stage").classList.add("streaming");
    if (mode === "interact") attachInteractHandlers();
  };
  ws.onmessage = (event) => {
    const frameImg = $("live-frame");
    const url = URL.createObjectURL(event.data);
    frameImg.src = url;
    if (lastFrameUrl) URL.revokeObjectURL(lastFrameUrl);
    lastFrameUrl = url;
  };
  ws.onclose = (event) => {
    logLine(`live-view closed (${event.code}${event.reason ? ": " + event.reason : ""})`);
    teardownLiveView();
  };
  ws.onerror = () => logLine("live-view error");
}

function disconnectLiveView() {
  if (ws) ws.close();
  teardownLiveView();
}

function teardownLiveView() {
  ws = null;
  detachInteractHandlers();
  $("connect-btn").textContent = "Connect";
  $("live-stage").classList.remove("streaming");
}

$("connect-btn").addEventListener("click", () => {
  if (ws) disconnectLiveView();
  else connectLiveView();
});

function frameToImageCoords(clientX, clientY) {
  const img = $("live-frame");
  const rect = img.getBoundingClientRect();
  const scaleX = img.naturalWidth / rect.width;
  const scaleY = img.naturalHeight / rect.height;
  return { x: (clientX - rect.left) * scaleX, y: (clientY - rect.top) * scaleY };
}

function sendInput(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

let interactHandlers = null;

function attachInteractHandlers() {
  const img = $("live-frame");
  const stage = $("live-stage");

  const onMouseMove = (e) => {
    const { x, y } = frameToImageCoords(e.clientX, e.clientY);
    sendInput({ kind: "mousemove", x, y });
  };
  const onMouseDown = (e) => {
    const { x, y } = frameToImageCoords(e.clientX, e.clientY);
    sendInput({ kind: "mousedown", x, y, button: "left" });
  };
  const onMouseUp = (e) => {
    const { x, y } = frameToImageCoords(e.clientX, e.clientY);
    sendInput({ kind: "mouseup", x, y, button: "left" });
  };
  const onWheel = (e) => {
    const { x, y } = frameToImageCoords(e.clientX, e.clientY);
    sendInput({ kind: "wheel", x, y, deltaX: e.deltaX, deltaY: e.deltaY });
    e.preventDefault();
  };
  const onKeyDown = (e) => {
    sendInput({ kind: "keydown", key: e.key });
    e.preventDefault();
  };
  const onKeyUp = (e) => {
    sendInput({ kind: "keyup", key: e.key });
    e.preventDefault();
  };

  img.addEventListener("mousemove", onMouseMove);
  img.addEventListener("mousedown", onMouseDown);
  img.addEventListener("mouseup", onMouseUp);
  img.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("keydown", onKeyDown);
  stage.addEventListener("keyup", onKeyUp);
  stage.focus();

  interactHandlers = { img, stage, onMouseMove, onMouseDown, onMouseUp, onWheel, onKeyDown, onKeyUp };
}

function detachInteractHandlers() {
  if (!interactHandlers) return;
  const { img, stage, onMouseMove, onMouseDown, onMouseUp, onWheel, onKeyDown, onKeyUp } =
    interactHandlers;
  img.removeEventListener("mousemove", onMouseMove);
  img.removeEventListener("mousedown", onMouseDown);
  img.removeEventListener("mouseup", onMouseUp);
  img.removeEventListener("wheel", onWheel);
  stage.removeEventListener("keydown", onKeyDown);
  stage.removeEventListener("keyup", onKeyUp);
  interactHandlers = null;
}
