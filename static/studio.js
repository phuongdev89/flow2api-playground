// Video Studio Flow — canvas genealogy tree port vanilla JS từ tool CINEMAFLOW
// trên labs.google Flow. Bố cục đúng tool gốc (computeInitialLayout):
//   hàng trên: [Cấu hình ảnh] [Nút nhân vật] [GỐC GIA PHẢ] [Cấu hình video]
//   dưới root fan-out N cột, mỗi cột đọc dọc: BRANCH → START → END → VIDEO
// Thẻ kéo được, canvas pan/zoom/fit, đường nối SVG dashed. Logic generation
// (planner + job queue + chain END→START + import/export) dùng lại pipeline
// playground y như trước.

const LS_KEY = 'studio_state_v1';
const LS_CANVAS_KEY = 'studio_canvas_v1';

// ── Kích thước layout (toạ độ world) ────────────────────────────────────
const NODE_W = 320;
const COL_GAP = 70;
const TOP_Y = 40;
const H_ROOT = 340;
const H_BRANCH = 160;
const H_FRAME = 350;
const H_VIDEO = 350;
const TIER_BRANCH_Y = TOP_Y + H_ROOT + 90;
const TIER_START_Y = TIER_BRANCH_Y + H_BRANCH + 60;
const TIER_END_Y = TIER_START_Y + H_FRAME + 60;
const TIER_VIDEO_Y = TIER_END_Y + H_FRAME + 60;

const $ = (id) => document.getElementById(id);

const els = {
  gate: $('gate'),
  app: $('app'),
  world: $('world'),
  edges: $('edges'),
  viewport: $('app'),
  toast: $('toast'),
  plannerBadge: $('planner-badge'),
  gateRetry: $('gate-retry'),
  progress: $('progress'),
  boardTitle: $('board-title'),
  zoomIn: $('zoom-in'),
  zoomOut: $('zoom-out'),
  zoomLabel: $('zoom-label'),
  zoomFit: $('zoom-fit'),
  rulesButton: $('rules-button'),
  rulesDialog: $('rules-dialog'),
  rulesClose: $('rules-close'),
  canvasHint: $('canvas-hint'),
};

// ── State ────────────────────────────────────────────────────────────────

const state = {
  projectId: null,
  storyboard: null,
  scenes: [],       // runtime: {startItem, endItem, videoItem, busy, error, _listeners}
  characters: [],   // ≤2 item id
  imageModels: [],
  videoModels: [],
  config: {
    imageModel: '',
    imageRes: '1K',
    videoModel: 'omni-1.2-flash',
    autoChain: true,
    autoRun: false,
  },
  transform: { x: 40, y: 40, zoom: 0.8 },
  nodePos: {},      // key → {x, y} — root/config/character/config_video + scene nodes
  running: false,
};

function saveState() {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify({
      projectId: state.projectId,
      storyboard: state.storyboard,
      scenes: state.scenes.map(({ startItem, endItem, videoItem }) => ({
        startItem, endItem, videoItem,
      })),
      characters: state.characters,
      config: state.config,
    }));
    localStorage.setItem(LS_CANVAS_KEY, JSON.stringify({
      transform: state.transform,
      pos: state.nodePos,
    }));
  } catch { /* quota — bỏ qua */ }
}

function loadState() {
  try {
    const data = JSON.parse(localStorage.getItem(LS_KEY) || 'null');
    if (data) {
      state.storyboard = data.storyboard || null;
      state.scenes = Array.isArray(data.scenes) ? data.scenes : [];
      state.characters = Array.isArray(data.characters) ? data.characters : [];
      Object.assign(state.config, data.config || {});
    }
  } catch { /* bỏ qua */ }
  try {
    const canvas = JSON.parse(localStorage.getItem(LS_CANVAS_KEY) || 'null');
    if (canvas?.transform) Object.assign(state.transform, canvas.transform);
    if (canvas?.pos) state.nodePos = canvas.pos;
  } catch { /* bỏ qua */ }
  state.transform.zoom = Math.min(1.8, Math.max(0.25, state.transform.zoom));
}

// ── HTTP helper (cùng quy ước playground.js) ────────────────────────────

async function request(path, options = {}) {
  const headers = {
    'X-Playground-Request': '1',
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...options.headers,
  };
  return fetch(path, { ...options, headers, credentials: 'same-origin', cache: 'no-store' });
}

let toastTimer = null;
function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle('error', isError);
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 4200);
}

async function requestOr401(path, options) {
  const response = await request(path, options);
  if (response.status === 401 || response.status === 403) {
    showGate();
    let detail = '';
    try {
      const data = await response.json();
      if (data?.detail) detail = ` — ${data.detail}`;
    } catch { /* body rỗng */ }
    throw new Error(`Phiên hết hạn hoặc request bị chặn${detail}. Đăng nhập lại ở Playground.`);
  }
  return response;
}

function showGate() {
  els.app.hidden = true;
  els.gate.hidden = false;
}

// ── Boot ────────────────────────────────────────────────────────────────

async function boot() {
  loadState();
  // Áp transform đã lưu SAU khi load state — applyTransform có saveState,
  // gọi trước loadState sẽ ghi đè LS bằng state nguyên bản (mất kịch bản).
  applyTransform();
  const session = await request('/playground/session');
  if (!session.ok) { showGate(); return; }
  els.gate.hidden = true;
  els.app.hidden = false;

  try {
    const wsResponse = await requestOr401('/playground/api/studio/workspace', { method: 'POST' });
    const workspace = await wsResponse.json();
    state.projectId = workspace.project.id;
    updatePlannerBadge(workspace.llm_ready);
    const project = await fetchStudioProject();
    state._projectResults = project?.results || [];
    indexItems(state._projectResults);
    reconcileStateWithProject(state._projectResults);
  } catch (error) {
    toast(error.message, true);
    return;
  }

  // Đồng bộ UI với state đã load từ localStorage.
  if (state.storyboard) els.boardTitle.textContent = `— ${state.storyboard.topic}`;

  await loadGenOptions();
  buildCanvas();
  fitView();
  updateProgress();
}

function updatePlannerBadge(llmReady) {
  els.plannerBadge.hidden = false;
  els.plannerBadge.textContent = llmReady
    ? 'Planner: LLM (STUDIO_LLM_MODEL)'
    : 'Planner: engine mẫu (điền .env STUDIO_LLM_* để dùng LLM)';
  els.plannerBadge.classList.toggle('llm', llmReady);
  els.plannerBadge.classList.toggle('template', !llmReady);
}

async function fetchStudioProject() {
  // Endpoint đơn /projects/{id} không tồn tại (405) — lấy qua list rồi find.
  const response = await requestOr401('/playground/api/projects');
  const projects = await response.json();
  return (Array.isArray(projects) ? projects : []).find(
    (project) => project.id === state.projectId,
  ) || null;
}

// Item trong project có thể bị xoá từ tab playground — cắt tham chiếu chết.
function reconcileStateWithProject(results) {
  const byId = new Map(results.map((item) => [String(item.id), item]));
  state.characters = state.characters.filter((id) => byId.has(id));
  for (const scene of state.scenes) {
    for (const key of ['startItem', 'endItem', 'videoItem']) {
      if (scene[key] && !byId.get(scene[key])) scene[key] = null;
    }
  }
}

function indexItems(results) {
  state._items = new Map((results || []).map((item) => [String(item.id), item]));
}

function lookupItem(id) {
  return state._items?.get(String(id)) || null;
}

function trackItem(item) {
  if (!state._items) state._items = new Map();
  state._items.set(String(item.id), item);
  if (!Array.isArray(state._projectResults)) state._projectResults = [];
  state._projectResults.push(item);
}

async function loadGenOptions() {
  let options = null;
  try {
    const response = await requestOr401('/playground/api/gen-options');
    if (response.ok) options = await response.json();
  } catch { /* fallback dưới */ }

  state.imageModels = options?.image?.models?.length
    ? options.image.models
    : [{ id: 'nano-banana-pro', label: 'Nano Banana Pro', base: 'nano-banana-pro', res: ['1K'] }];
  state.videoModels = options?.video?.models?.length
    ? options.video.models
    : [{ id: 'veo-3.1-lite', label: 'Veo 3.1 - Lite', length: [4, 6, 8], frames: true }];

  if (!state.imageModels.some((m) => m.id === state.config.imageModel)) {
    const preferred = state.imageModels.find((m) => /flash-image$/.test(m.id))
      || state.imageModels.find((m) => !/lite/.test(m.id))
      || state.imageModels[0];
    state.config.imageModel = preferred?.id || '';
  }
  if (!state.videoModels.some((m) => m.id === state.config.videoModel)) {
    const omni = state.videoModels.find((m) => m.id.startsWith('omni-1.2'));
    state.config.videoModel = (omni || state.videoModels[0])?.id || '';
  }
}

function imageModelSpec() {
  return state.imageModels.find((m) => m.id === state.config.imageModel) || null;
}
function videoModelSpec() {
  return state.videoModels.find((m) => m.id === state.config.videoModel) || null;
}
function imageResOptions() {
  const model = imageModelSpec();
  return model?.res?.length ? model.res : ['1K'];
}

function nearestLength(lengths, wanted) {
  return lengths.reduce((best, value) => (
    Math.abs(value - wanted) < Math.abs(best - wanted) ? value : best
  ), lengths[0]);
}

// ── Layout (port computeInitialLayout của tool gốc) ─────────────────────

function defaultLayout(sceneCount) {
  const pos = {};
  const configX = 60;
  const charX = configX + NODE_W + COL_GAP;
  const startX = charX + NODE_W + COL_GAP; // 840
  const columnsWidth = sceneCount * NODE_W + Math.max(0, sceneCount - 1) * COL_GAP;
  const rootX = Math.max(startX, startX + (columnsWidth - NODE_W) / 2);

  pos.root = { x: rootX, y: TOP_Y };
  pos.config = { x: configX, y: TOP_Y };
  pos.character = { x: Math.max(charX, rootX - NODE_W - COL_GAP), y: TOP_Y };
  pos['config_video'] = { x: rootX + NODE_W + COL_GAP, y: TOP_Y };
  for (let i = 0; i < sceneCount; i += 1) {
    const colX = startX + i * (NODE_W + COL_GAP);
    pos[`branch_${i}`] = { x: colX, y: TIER_BRANCH_Y };
    pos[`start_${i}`] = { x: colX, y: TIER_START_Y };
    pos[`end_${i}`] = { x: colX, y: TIER_END_Y };
    pos[`video_${i}`] = { x: colX, y: TIER_VIDEO_Y };
  }
  return pos;
}

function nodeHeight(key) {
  if (key === 'root') return H_ROOT;
  if (key.startsWith('branch_')) return H_BRANCH;
  if (key.startsWith('video_')) return H_VIDEO;
  if (key.startsWith('start_') || key.startsWith('end_')) return H_FRAME;
  return H_ROOT;
}

function sceneCount() {
  return state.storyboard?.scenes?.length || 0;
}

// ── Canvas transform (pan/zoom/fit) ─────────────────────────────────────

function applyTransform() {
  els.world.style.transform =
    `translate(${state.transform.x}px, ${state.transform.y}px) scale(${state.transform.zoom})`;
  els.zoomLabel.textContent = `${Math.round(state.transform.zoom * 100)}%`;
  saveState();
}

function zoomAt(cx, cy, factor) {
  const t = state.transform;
  const zoom = Math.min(1.8, Math.max(0.25, t.zoom * factor));
  const k = zoom / t.zoom;
  t.x = cx - (cx - t.x) * k;
  t.y = cy - (cy - t.y) * k;
  t.zoom = zoom;
  applyTransform();
}

function fitView() {
  const keys = Object.keys(state.nodePos);
  if (!keys.length) {
    state.transform = { x: 40, y: 40, zoom: 0.8 };
    applyTransform();
    return;
  }
  let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
  for (const key of keys) {
    const p = state.nodePos[key];
    minX = Math.min(minX, p.x);
    minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + NODE_W);
    maxY = Math.max(maxY, p.y + nodeHeight(key));
  }
  const vw = els.viewport.clientWidth;
  const vh = els.viewport.clientHeight;
  const zoom = Math.min(1, Math.max(0.25, Math.min(vw / (maxX - minX + 80), vh / (maxY - minY + 80))));
  state.transform = {
    x: Math.max(20, (vw - (maxX - minX) * zoom) / 2 - minX * zoom),
    y: Math.max(20, (vh - (maxY - minY) * zoom) / 2 - minY * zoom),
    zoom,
  };
  applyTransform();
}

function wireCanvasControls() {
  // Pan bằng kéo nền (không phải trên node).
  let panState = null;
  els.viewport.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.node')) return;
    panState = { x: event.clientX, y: event.clientY, tx: state.transform.x, ty: state.transform.y };
    els.viewport.classList.add('panning');
    els.viewport.setPointerCapture(event.pointerId);
  });
  els.viewport.addEventListener('pointermove', (event) => {
    if (!panState) return;
    state.transform.x = panState.tx + (event.clientX - panState.x);
    state.transform.y = panState.ty + (event.clientY - panState.y);
    applyTransform();
  });
  const endPan = (event) => {
    if (!panState) return;
    panState = null;
    els.viewport.classList.remove('panning');
    if (event.pointerId !== undefined) {
      try { els.viewport.releasePointerCapture(event.pointerId); } catch { /* hết capture */ }
    }
    saveState();
  };
  els.viewport.addEventListener('pointerup', endPan);
  els.viewport.addEventListener('pointercancel', endPan);

  // Zoom bằng wheel quanh con trỏ.
  els.viewport.addEventListener('wheel', (event) => {
    event.preventDefault();
    const rect = els.viewport.getBoundingClientRect();
    zoomAt(event.clientX - rect.left, event.clientY - rect.top, Math.exp(-event.deltaY * 0.0015));
  }, { passive: false });

  const rectCenter = () => {
    const rect = els.viewport.getBoundingClientRect();
    return { cx: rect.width / 2, cy: rect.height / 2 };
  };
  els.zoomIn.addEventListener('click', () => { const { cx, cy } = rectCenter(); zoomAt(cx, cy, 1.15); });
  els.zoomOut.addEventListener('click', () => { const { cx, cy } = rectCenter(); zoomAt(cx, cy, 1 / 1.15); });
  els.zoomLabel.addEventListener('click', () => { const { cx, cy } = rectCenter(); zoomAt(cx, cy, 1 / state.transform.zoom); });
  els.zoomFit.addEventListener('click', fitView);

  els.rulesButton.addEventListener('click', () => els.rulesDialog.showModal());
  els.rulesClose.addEventListener('click', () => els.rulesDialog.close());
}

// ── Drag node ───────────────────────────────────────────────────────────

function makeDraggable(nodeEl, key) {
  const head = nodeEl.querySelector('.node-head');
  head.addEventListener('pointerdown', (event) => {
    if (event.target.closest('button, select, input, textarea, a')) return;
    event.preventDefault();
    const start = { x: event.clientX, y: event.clientY, px: state.nodePos[key].x, py: state.nodePos[key].y };
    nodeEl.classList.add('dragging');
    try { head.setPointerCapture(event.pointerId); } catch { /* pointer giả/synthetic */ }
    const move = (moveEvent) => {
      state.nodePos[key] = {
        x: start.px + (moveEvent.clientX - start.x) / state.transform.zoom,
        y: start.py + (moveEvent.clientY - start.y) / state.transform.zoom,
      };
      nodeEl.style.left = `${state.nodePos[key].x}px`;
      nodeEl.style.top = `${state.nodePos[key].y}px`;
      updateEdges();
    };
    const up = () => {
      nodeEl.classList.remove('dragging');
      head.removeEventListener('pointermove', move);
      head.removeEventListener('pointerup', up);
      head.removeEventListener('pointercancel', up);
      saveState();
    };
    head.addEventListener('pointermove', move);
    head.addEventListener('pointerup', up);
    head.addEventListener('pointercancel', up);
  });
}

function mountNode(key, accent, icon, title, badge) {
  let nodeEl = els.world.querySelector(`[data-node="${key}"]`);
  if (nodeEl) return nodeEl;
  nodeEl = document.createElement('section');
  nodeEl.className = `node node-accent-${accent}`;
  nodeEl.dataset.node = key;
  const pos = state.nodePos[key] || { x: 0, y: 0 };
  nodeEl.style.left = `${pos.x}px`;
  nodeEl.style.top = `${pos.y}px`;
  nodeEl.innerHTML =
    `<header class="node-head"><span class="head-icon">${icon}</span>` +
    `<span class="head-title">${title}</span>` +
    (badge ? `<span class="head-badge">${badge}</span>` : '') +
    '</header><div class="node-body"></div>';
  els.world.appendChild(nodeEl);
  makeDraggable(nodeEl, key);
  return nodeEl;
}

// ── Edges (SVG connectors) ──────────────────────────────────────────────

function edgeAnchor(key, side) {
  const p = state.nodePos[key] || { x: 0, y: 0 };
  const h = nodeHeight(key);
  switch (side) {
    case 'top': return [p.x + NODE_W / 2, p.y];
    case 'bottom': return [p.x + NODE_W / 2, p.y + h];
    case 'left': return [p.x, p.y + Math.min(h / 2, 60)];
    default: return [p.x + NODE_W, p.y + Math.min(h / 2, 60)];
  }
}

function edgePath([x1, y1], [x2, y2]) {
  if (Math.abs(y2 - y1) >= Math.abs(x2 - x1)) {
    const mid = (y1 + y2) / 2;
    return `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`;
  }
  const mid = (x1 + x2) / 2;
  return `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`;
}

function edgeSpecs() {
  const specs = [];
  // Hàng cấu hình: ảnh → nhân vật → root → video (luôn có)
  specs.push(['config', 'right', 'character', 'left', 'var(--cyan)', '6 4']);
  specs.push(['character', 'right', 'root', 'left', 'var(--pink)', '6 4']);
  specs.push(['root', 'right', 'config_video', 'left', 'var(--violet)', '6 4']);
  // Root fan-out → từng cột + chain END → START
  for (let i = 0; i < sceneCount(); i += 1) {
    specs.push(['root', 'bottom', `branch_${i}`, 'top', 'var(--accent)', '5 5']);
    specs.push([`branch_${i}`, 'bottom', `start_${i}`, 'top', '#3a3a52', '4 4']);
    specs.push([`start_${i}`, 'bottom', `end_${i}`, 'top', '#3a3a52', '4 4']);
    specs.push([`end_${i}`, 'bottom', `video_${i}`, 'top', '#3a3a52', '4 4']);
    if (state.config.autoChain && i > 0) {
      specs.push([`end_${i - 1}`, 'right', `start_${i}`, 'left', 'var(--green)', '5 3']);
    }
  }
  return specs;
}

function updateEdges() {
  const svg = els.edges;
  const parts = [];
  for (const [from, fromSide, to, toSide, color, dash] of edgeSpecs()) {
    const [x1, y1] = edgeAnchor(from, fromSide);
    const [x2, y2] = edgeAnchor(to, toSide);
    parts.push(
      `<path d="${edgePath([x1, y1], [x2, y2])}" fill="none" stroke="${color}"` +
      ` stroke-width="1.6" stroke-dasharray="${dash}" opacity="0.75"/>` +
      `<circle cx="${x1}" cy="${y1}" r="3.5" fill="${color}"/>` +
      `<circle cx="${x2}" cy="${y2}" r="3.5" fill="${color}"/>`,
    );
  }
  svg.innerHTML = parts.join('');
}

// ── Build canvas ────────────────────────────────────────────────────────

function buildCanvas() {
  els.world.querySelectorAll('.node').forEach((node) => node.remove());
  const count = sceneCount();
  const defaults = defaultLayout(count);
  // Vị trí root/config giữ nguyên user đã kéo; toạ độ cột cảnh luôn tính lại
  // theo số cảnh hiện tại (plan mới/Xoá kịch bản). Prune key cảnh đã thừa.
  for (const key of Object.keys(state.nodePos)) {
    if (/^(branch|start|end|video)_/.test(key)) {
      const index = Number(key.split('_')[1]);
      if (!state.storyboard || Number.isNaN(index) || index >= count) delete state.nodePos[key];
    }
  }
  for (const key of Object.keys(defaults)) {
    // Giữ vị trí user đã kéo (mọi key); chỉ đổ layout mặc định cho key còn thiếu.
    if (!state.nodePos[key]) state.nodePos[key] = defaults[key];
  }

  buildStaticNodes();
  for (let i = 0; i < count; i += 1) {
    buildSceneNodes(i);
  }
  els.canvasHint.hidden = count > 0;
  updateEdges();
}

function buildStaticNodes() {
  buildRootNode();
  buildImageConfigNode();
  buildCharacterNode();
  buildVideoConfigNode();
}

// ── Root node ───────────────────────────────────────────────────────────

function buildRootNode() {
  const node = mountNode('root', 'root', '🎯', 'GỐC GIA PHẢ (TOP ROOT)', 'Centered Root');
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <label for="topic">Hôm nay sản xuất gì</label>
    <textarea id="topic" rows="3" placeholder="Nhập bất kỳ chủ đề sáng tạo nào bạn muốn sản xuất...">Golden artisan pancakes, culinary perfection</textarea>
    <div class="count-row">
      <label for="scene-count" style="margin:0">Số lượng cảnh (N scenes)</label>
      <span class="count-chip" id="scene-count-out">8 cảnh</span>
    </div>
    <input id="scene-count" type="range" min="4" max="25" value="8">
    <div class="range-ticks"><span>Tối thiểu 4</span><span>Mặc định 8</span><span>Tối đa 25</span></div>
    <button id="plan-button" class="primary plan-button" type="button">✨ Lên kịch bản (Fan out N cảnh)</button>
    <div class="btn-row">
      <button id="import-button" type="button">Import JSON</button>
      <button id="export-button" type="button">Export JSON</button>
      <button id="reset-button" class="danger" type="button">Xoá kịch bản</button>
    </div>
    <input id="import-file" type="file" accept="application/json,.json" hidden>`;
  if (state.storyboard) $('topic').value = state.storyboard.topic || $('topic').value;

  $('scene-count').addEventListener('input', () => {
    $('scene-count-out').textContent = `${$('scene-count').value} cảnh`;
  });
  $('plan-button').addEventListener('click', plan);
  $('import-button').addEventListener('click', () => $('import-file').click());
  $('import-file').addEventListener('change', () => {
    const file = $('import-file').files?.[0];
    $('import-file').value = '';
    if (file) importJson(file);
  });
  $('export-button').addEventListener('click', exportJson);
  $('reset-button').addEventListener('click', resetBoard);
}

// ── Config image node ───────────────────────────────────────────────────

function buildImageConfigNode() {
  const node = mountNode('config', 'config', '🎨', 'CẤU HÌNH SINH ẢNH', 'Node Config');
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <div id="image-model-list"></div>
    <label for="image-res">Res ảnh</label>
    <select id="image-res"></select>
    <label class="check"><input id="auto-chain" type="checkbox"> Tự động tái dùng ảnh END trước làm START</label>
    <p class="check-hint">🔗 Tắt thì bấm "Tái dùng" thủ công ở từng node START.</p>
    <label class="check"><input id="auto-run" type="checkbox"> Tự động gen ảnh tuần tự</label>
    <p class="check-hint">▶ "Sản xuất toàn bộ" chạy lần lượt từng cảnh START → END → VIDEO.</p>
    <button id="run-all" class="primary plan-button" type="button">▶ Sản xuất toàn bộ</button>`;
  renderImageModelOptions();
  renderResOptions();

  $('auto-chain').checked = state.config.autoChain;
  $('auto-chain').addEventListener('change', () => {
    state.config.autoChain = $('auto-chain').checked;
    renderSceneValues();
    updateEdges();
    saveState();
  });
  $('auto-run').checked = state.config.autoRun;
  $('auto-run').addEventListener('change', () => {
    state.config.autoRun = $('auto-run').checked;
    saveState();
  });
  $('run-all').addEventListener('click', runAllScenes);
}

function renderImageModelOptions() {
  const list = $('image-model-list');
  list.innerHTML = '<label style="margin-top:0">Model tạo ảnh AI</label>';
  for (const model of state.imageModels) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'model-option' + (model.id === state.config.imageModel ? ' selected' : '');
    button.innerHTML =
      `<span class="model-name">🍌 ${model.label}</span><br>` +
      `<span class="model-desc">${model.id}</span>`;
    button.addEventListener('click', () => {
      state.config.imageModel = model.id;
      renderImageModelOptions();
      renderResOptions();
      saveState();
    });
    list.appendChild(button);
  }
}

function renderResOptions() {
  const select = $('image-res');
  fillSelect(select, imageResOptions().map((value) => ({ value, label: value })));
  if (!imageResOptions().includes(state.config.imageRes)) {
    state.config.imageRes = imageResOptions()[0];
  }
  select.value = state.config.imageRes;
  select.onchange = () => {
    state.config.imageRes = select.value;
    saveState();
  };
}

// ── Character node ──────────────────────────────────────────────────────

function buildCharacterNode() {
  const node = mountNode('character', 'char', '🧑‍🎭', 'NÚT NHÂN VẬT', 'Nối gốc gia phả');
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <p class="hint" style="margin-top:0">Chọn hoặc tải tối đa <b>2 ảnh nhân vật</b> — mọi frame được sinh sẽ i2i tham chiếu.</p>
    <div class="char-grid" id="character-list"></div>
    <div class="btn-two">
      <select id="character-pick"><option value="">— Chọn ảnh từ project —</option></select>
      <button id="character-add" class="ghost char-add-btn" type="button">Thêm</button>
    </div>
    <label class="file-drop" for="character-file">…hoặc tải ảnh lên (PNG/JPEG/WebP)</label>
    <input id="character-file" type="file" accept="image/png,image/jpeg,image/webp" hidden>`;
  renderCharacters();
  $('character-add').addEventListener('click', addCharacterFromPicker);
  $('character-file').addEventListener('change', () => {
    const file = $('character-file').files?.[0];
    $('character-file').value = '';
    if (file) addCharacterFromFile(file);
  });
}

function renderCharacters() {
  const grid = $('character-list');
  if (!grid) return;
  grid.innerHTML = '';
  if (!state.characters.length) {
    const empty = document.createElement('div');
    empty.className = 'char-empty';
    empty.textContent = 'Chưa chọn ảnh nhân vật — thêm tối đa 2 ảnh từ thư viện hoặc máy tính';
    grid.appendChild(empty);
  }
  for (const id of state.characters) {
    const item = lookupItem(id);
    const wrap = document.createElement('div');
    wrap.className = 'char-item';
    const img = document.createElement('img');
    if (item?.url) img.src = item.url;
    img.alt = item?.name || 'Nhân vật';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', 'Bỏ ảnh nhân vật');
    remove.addEventListener('click', () => {
      state.characters = state.characters.filter((value) => value !== id);
      renderCharacters();
      saveState();
    });
    wrap.append(img, remove);
    grid.appendChild(wrap);
  }
  refreshCharacterPicker();
}

async function refreshCharacterPicker() {
  const pick = $('character-pick');
  if (!pick || !state.projectId) return;
  try {
    const project = await fetchStudioProject();
    if (!project) return;
    const candidates = (project.results || []).filter(
      (item) => item.kind === 'image' && !state.characters.includes(String(item.id)),
    );
    fillSelect(
      pick,
      [{ value: '', label: '— Chọn ảnh từ project —' }].concat(
        candidates.slice(-40).reverse().map((item) => ({
          value: String(item.id),
          label: `${item.name || 'ảnh'} ${item.meta?.source === 'playground-reference' ? '(ref)' : ''}`.trim(),
        })),
      ),
    );
  } catch { /* lần sau */ }
}

// ── Video config node ───────────────────────────────────────────────────

function buildVideoConfigNode() {
  const node = mountNode('config_video', 'video', '🎬', 'CẤU HÌNH SINH VIDEO', 'Video Config');
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <label style="margin-top:0">Mô hình tạo video AI (I2V First/Last)</label>
    <div id="video-model-list"></div>
    <p class="hint" id="video-model-hint"></p>`;
  renderVideoModelOptions();
}

function renderVideoModelOptions() {
  const list = $('video-model-list');
  if (!list) return;
  list.innerHTML = '';
  for (const model of state.videoModels) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'model-option video' + (model.id === state.config.videoModel ? ' selected' : '');
    const lengths = model.length?.length ? model.length.join('·') + 's' : 'mặc định engine';
    button.innerHTML =
      `<span class="model-name">🎞 ${model.label}</span><br>` +
      `<span class="model-desc">Thời lượng: ${lengths} · 720p</span>`;
    button.addEventListener('click', () => {
      state.config.videoModel = model.id;
      renderVideoModelOptions();
      renderSceneValues();
      clampPlanDurations();
      updateEdges();
      saveState();
    });
    list.appendChild(button);
  }
  const spec = videoModelSpec();
  const hint = $('video-model-hint');
  if (hint && spec) {
    hint.textContent = spec.length?.length
      ? `Thời lượng ${spec.length.join('·')}s — chỉnh riêng ở từng thẻ phân cảnh.`
      : 'Thời lượng theo mặc định engine.';
  }
}

function clampPlanDurations() {
  const lengths = videoModelSpec()?.length || [];
  if (!lengths.length || !state.storyboard) return;
  for (const scene of state.storyboard.scenes) {
    if (!lengths.includes(scene.durationSeconds)) {
      scene.durationSeconds = nearestLength(lengths, scene.durationSeconds);
    }
  }
  renderSceneValues();
  saveState();
}

// ── Scene nodes ─────────────────────────────────────────────────────────

const ROLE_LABEL = { hook: 'Hook', setup: 'Setup', body: 'Body', reward: 'Reward' };

function buildSceneNodes(index) {
  const plan = state.storyboard.scenes[index];
  const runtime = state.scenes[index] || (state.scenes[index] = {
    startItem: null, endItem: null, videoItem: null, busy: false, error: null,
  });

  // BRANCH — thẻ phân cảnh
  const branch = mountNode(`branch_${index}`, 'branch', '🌿', `<span class="scene-id">S${String(index + 1).padStart(2, '0')}</span>`, '');
  const branchBody = branch.querySelector('.node-body');
  branchBody.innerHTML = `
    <div class="scene-head-row">
      <span class="role-badge ${plan.role}">${ROLE_LABEL[plan.role] || plan.role}</span>
      <span style="flex:1"></span>
      <select class="duration-select" aria-label="Thời lượng cảnh"></select>
    </div>
    <div class="scene-name" title="${escapeHtml(plan.nameVi)}">${escapeHtml(plan.nameVi)}</div>
    <details class="scene-prompts">
      <summary>Prompt (sửa trực tiếp)</summary>
      <label>START<textarea data-prompt="startPrompt"></textarea></label>
      <label>END<textarea data-prompt="endPrompt"></textarea></label>
      <label>VIDEO<textarea data-prompt="videoPrompt"></textarea></label>
    </details>`;
  wireBranchNode(index, branchBody);

  buildFrameNode(index, runtime, 'start', 'START');
  buildFrameNode(index, runtime, 'end', 'END');
  buildVideoNode(index, runtime);
  updateEdges();
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

function wireBranchNode(index, body) {
  const plan = state.storyboard.scenes[index];
  const lengths = videoModelSpec()?.length || [];
  const select = body.querySelector('.duration-select');
  if (lengths.length) {
    fillSelect(select, lengths.map((value) => ({ value: String(value), label: `${value}s` })));
  } else {
    fillSelect(select, [String(plan.durationSeconds)].map((value) => ({ value, label: `${value}s` })));
  }
  select.addEventListener('change', () => {
    plan.durationSeconds = Number(select.value);
    saveState();
  });
  body.querySelectorAll('textarea[data-prompt]').forEach((textarea) => {
    textarea.value = plan[textarea.dataset.prompt] || '';
    textarea.addEventListener('change', () => {
      plan[textarea.dataset.prompt] = textarea.value.trim();
      saveState();
    });
  });
}

function buildFrameNode(index, runtime, slot, label) {
  const accent = slot === 'start' ? 'start' : 'end';
  const node = mountNode(
    `${slot}_${index}`, accent,
    `<span class="scene-id">S${String(index + 1).padStart(2, '0')}</span>`,
    label, slot === 'start' ? 'Frame đầu' : 'Frame cuối',
  );
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <button class="slot-thumb" type="button" aria-label="Xem ${label} cảnh ${index + 1}">trống</button>
    <div class="slot-status"></div>
    <div class="slot-actions"></div>`;
  const thumb = body.querySelector('.slot-thumb');
  const status = body.querySelector('.slot-status');
  const actions = body.querySelector('.slot-actions');

  const refresh = () => {
    const itemId = runtime[`${slot}Item`];
    const item = lookupItem(itemId);
    thumb.innerHTML = '';
    if (item && item.kind === 'image') {
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = `${label} cảnh ${index + 1}`;
      thumb.appendChild(img);
    } else {
      thumb.textContent = itemId ? '…mất' : 'trống';
    }
    const chained = slot === 'start' && state.config.autoChain && index > 0
      && runtime.startItem
      && runtime.startItem === state.scenes[index - 1]?.endItem;
    thumb.classList.toggle('reused', Boolean(chained));
    thumb.title = chained ? 'Tái dùng END cảnh trước' : '';

    status.className = 'slot-status';
    if (runtime.busy && !runtime[`${slot}Item`]) {
      status.textContent = 'đang tạo…';
      status.classList.add('busy');
    } else if (runtime.error && !runtime[`${slot}Item`]) {
      status.textContent = runtime.error;
      status.classList.add('error');
    } else if (runtime[`${slot}Item`]) {
      status.textContent = 'xong';
      status.classList.add('ok');
    } else {
      status.textContent = '';
    }

    actions.innerHTML = '';
    if (slot === 'start' && state.config.autoChain && index > 0 && state.scenes[index - 1]?.endItem) {
      const reuse = document.createElement('button');
      reuse.type = 'button';
      reuse.className = 'slot-button';
      reuse.textContent = '🔗 Tái dùng END trước';
      reuse.addEventListener('click', () => {
        runtime.startItem = state.scenes[index - 1].endItem;
        saveState();
        rerenderScene(index);
      });
      actions.appendChild(reuse);
    } else {
      const gen = document.createElement('button');
      gen.type = 'button';
      gen.className = 'slot-button';
      gen.textContent = runtime[`${slot}Item`] ? 'Tạo lại' : 'Tạo';
      gen.disabled = state.running;
      gen.addEventListener('click', () => runSlot(index, slot));
      actions.appendChild(gen);
    }
  };
  thumb.addEventListener('click', () => {
    const item = lookupItem(runtime[`${slot}Item`]);
    if (item?.url) window.open(item.url, '_blank');
  });
  runtime._listeners = runtime._listeners || {};
  runtime._listeners[slot] = refresh;
  refresh();
}

function buildVideoNode(index, runtime) {
  const node = mountNode(
    `video_${index}`, 'video',
    `<span class="scene-id">S${String(index + 1).padStart(2, '0')}</span>`,
    'VIDEO', 'I2V First/Last',
  );
  const body = node.querySelector('.node-body');
  body.innerHTML = `
    <button class="slot-thumb" type="button" aria-label="Xem video cảnh ${index + 1}">trống</button>
    <div class="slot-status"></div>
    <div class="slot-actions"></div>`;
  const thumb = body.querySelector('.slot-thumb');
  const status = body.querySelector('.slot-status');
  const actions = body.querySelector('.slot-actions');

  const refresh = () => {
    const item = lookupItem(runtime.videoItem);
    thumb.innerHTML = '';
    if (item && item.kind === 'video') {
      const video = document.createElement('video');
      video.src = item?.url || '';
      video.muted = true;
      video.loop = true;
      video.playsInline = true;
      video.addEventListener('mouseenter', () => video.play().catch(() => {}));
      video.addEventListener('mouseleave', () => video.pause());
      thumb.appendChild(video);
    } else {
      thumb.textContent = runtime.videoItem ? '…mất' : 'START + END xong rồi Render';
    }
    status.className = 'slot-status';
    if (runtime.busy) {
      status.textContent = 'đang render…';
      status.classList.add('busy');
    } else if (runtime.error) {
      status.textContent = runtime.error;
      status.classList.add('error');
    } else if (runtime.videoItem) {
      status.textContent = 'xong';
      status.classList.add('ok');
    } else {
      status.textContent = '';
    }
    actions.innerHTML = '';
    const render = document.createElement('button');
    render.type = 'button';
    render.className = 'slot-button primary';
    render.textContent = runtime.videoItem ? 'Render lại' : 'Render';
    render.disabled = !(runtime.startItem && runtime.endItem) || state.running;
    render.addEventListener('click', () => runScene(index));
    actions.appendChild(render);
  };
  thumb.addEventListener('click', () => {
    const item = lookupItem(runtime.videoItem);
    if (item?.url) window.open(item.url, '_blank');
  });
  runtime._listeners = runtime._listeners || {};
  runtime._listeners.video = refresh;
  refresh();
}

function rerenderScene(index) {
  const runtime = state.scenes[index];
  if (!runtime?._listeners) return;
  for (const slot of ['start', 'end', 'video']) {
    runtime._listeners[slot]?.();
  }
  updateEdges();
}

function renderSceneValues() {
  // Đổi model video/toggle chain → dựng lại nội dung node (innerHTML mới nên
  // listener không bị nhân đôi) và vẽ lại chain END→START. Guard: scene node
  // chỉ mount khi layout đã có (buildCanvas chạy rồi) — không thì bỏ qua,
  // buildCanvas sẽ dựng sau với toạ độ đúng.
  if (!state.storyboard) return;
  state.storyboard.scenes.forEach((plan, index) => {
    if (!state.nodePos[`branch_${index}`]) return;
    buildSceneNodes(index);
  });
  updateEdges();
}

function fillSelect(select, options) {
  select.innerHTML = '';
  for (const option of options) {
    const node = document.createElement('option');
    node.value = option.value;
    node.textContent = option.label;
    select.appendChild(node);
  }
}

// ── Planner ─────────────────────────────────────────────────────────────

async function plan() {
  const topic = $('topic').value.trim();
  if (!topic) { toast('Nhập chủ đề trước đã.', true); return; }
  const sceneCountValue = Number($('scene-count').value);
  const button = $('plan-button');
  button.disabled = true;
  button.textContent = 'Đang lên kịch bản…';
  try {
    const response = await requestOr401('/playground/api/studio/plan', {
      method: 'POST',
      body: JSON.stringify({ topic, scene_count: sceneCountValue }),
    });
    const data = await response.json();
    if (!response.ok) { toast(data.detail || data.error || 'Lên kịch bản thất bại.', true); return; }
    applyStoryboard(data.storyboard);
    toast(`Đã lên ${data.storyboard.scenes.length} cảnh (${data.source === 'llm' ? 'LLM' : 'engine mẫu'}).`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = '✨ Lên kịch bản (Fan out N cảnh)';
  }
}

function applyStoryboard(storyboard) {
  state.storyboard = storyboard;
  state.scenes = storyboard.scenes.map(() => ({
    startItem: null, endItem: null, videoItem: null, busy: false, error: null,
  }));
  // buildCanvas TRƯỚC: tính layout + mount node theo nodePos, rồi mới clamp
  // duration (clamp dựng lại node — cần layout sẵn để không mount tại (0,0)).
  els.boardTitle.textContent = `— ${storyboard.topic}`;
  buildCanvas();
  clampPlanDurations();
  fitView();
  updateProgress();
  saveState();
}

// ── Import / Export (schema tool gốc trên Flow) ─────────────────────────

function exportJson() {
  if (!state.storyboard) { toast('Chưa có kịch bản để export.', true); return; }
  const blob = new Blob([JSON.stringify(state.storyboard, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `video-studio-flow-${new Date().toISOString().slice(0, 10)}.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

async function importJson(file) {
  let raw;
  try {
    raw = JSON.parse(await file.text());
  } catch {
    toast('File JSON không đọc được.', true);
    return;
  }
  try {
    const response = await requestOr401('/playground/api/studio/import', {
      method: 'POST',
      body: JSON.stringify({ payload: raw, topic: String(raw?.topic || '') }),
    });
    const data = await response.json();
    if (!response.ok) { toast(data.detail || 'Import thất bại.', true); return; }
    if (data.error) { toast(data.error, true); return; }
    applyStoryboard(data.storyboard);
    $('topic').value = data.storyboard.topic || '';
    toast(`Import xong ${data.storyboard.scenes.length} cảnh.`);
  } catch (error) {
    toast(error.message, true);
  }
}

function resetBoard() {
  if (!confirm('Xoá kịch bản hiện tại? (Ảnh/video trong project giữ nguyên)')) return;
  state.storyboard = null;
  state.scenes = [];
  for (const key of Object.keys(state.nodePos)) {
    if (/^(branch|start|end|video)_/.test(key)) delete state.nodePos[key];
  }
  els.boardTitle.textContent = '';
  buildCanvas();
  fitView();
  updateProgress();
  saveState();
}

// ── Generation orchestration ────────────────────────────────────────────

function characterRefs() {
  return state.characters.slice(0, 2);
}

async function launchGenerate(payload) {
  const response = await requestOr401(
    `/playground/api/projects/${state.projectId}/generate`,
    { method: 'POST', body: JSON.stringify(payload) },
  );
  if (response.status === 429) {
    const data = await response.json();
    throw new Error(data.detail || 'Quá nhiều job đang chạy.');
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.detail || 'Không tạo được job.');
  }
  const data = await response.json();
  return data.job_id;
}

async function waitJob(jobId) {
  const deadline = Date.now() + 22 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 2500));
    const response = await requestOr401(
      `/playground/api/projects/${state.projectId}/generate/${jobId}`,
    );
    if (!response.ok) continue; // transient — poll tiếp
    const job = await response.json();
    if (job.status === 'done') return job.results || [];
    if (job.status === 'error') throw new Error(job.error || 'Job generation lỗi.');
  }
  throw new Error('Hết thời gian chờ job.');
}

async function runSlot(index, slot) {
  const runtime = state.scenes[index];
  const planScene = state.storyboard.scenes[index];
  if (!runtime || !planScene) return;

  // Chain END→START: cảnh sau lấy ảnh END của cảnh trước làm START.
  if (slot === 'start' && state.config.autoChain && index > 0 && state.scenes[index - 1].endItem) {
    runtime.startItem = state.scenes[index - 1].endItem;
    saveState();
    rerenderScene(index);
    return;
  }

  const model = imageModelSpec();
  if (!model) { toast('Chưa chọn được model ảnh.', true); return; }
  const prompt = slot === 'start' ? planScene.startPrompt : planScene.endPrompt;
  if (!prompt) { toast(`Thiếu prompt ${slot.toUpperCase()} cảnh ${index + 1}.`, true); return; }

  runtime.busy = true;
  runtime.error = null;
  rerenderScene(index);
  updateProgress();
  try {
    const payload = {
      mode: 'image',
      model: model.id,
      ratio: '9:16',
      qty: 1,
      res: $('image-res').value,
      length: null,
      vmode: 'text',
      prompt,
      references: characterRefs(),
    };
    const jobId = await launchGenerate(payload);
    const results = await waitJob(jobId);
    const item = results.find((entry) => entry.kind === 'image');
    if (!item) throw new Error('Job xong nhưng không có ảnh.');
    runtime[`${slot}Item`] = String(item.id);
    trackItem(item);
  } catch (error) {
    runtime.error = String(error.message || error).slice(0, 160);
    toast(`Cảnh ${index + 1} ${slot.toUpperCase()}: ${runtime.error}`, true);
  } finally {
    runtime.busy = false;
    rerenderScene(index);
    updateProgress();
    saveState();
  }
}

async function runScene(index) {
  const runtime = state.scenes[index];
  const planScene = state.storyboard.scenes[index];
  if (!runtime?.startItem || !runtime?.endItem) {
    toast('Cần đủ START + END trước khi render video.', true);
    return;
  }
  const spec = videoModelSpec();
  if (!spec) { toast('Chưa chọn được model video.', true); return; }

  const lengths = spec.length || [];
  const duration = lengths.length ? nearestLength(lengths, planScene.durationSeconds) : planScene.durationSeconds;
  planScene.durationSeconds = duration;

  runtime.busy = true;
  runtime.error = null;
  rerenderScene(index);
  updateProgress();
  try {
    const payload = {
      mode: 'video',
      model: spec.id,
      ratio: '9:16',
      qty: 1,
      res: '720p',
      length: lengths.length ? duration : null,
      vmode: 'frames',
      prompt: planScene.videoPrompt || planScene.stateChange || planScene.nameVi,
      references: [runtime.startItem, runtime.endItem].slice(0, 2),
    };
    const jobId = await launchGenerate(payload);
    const results = await waitJob(jobId);
    const item = results.find((entry) => entry.kind === 'video');
    if (!item) throw new Error('Job xong nhưng không có video.');
    runtime.videoItem = String(item.id);
    trackItem(item);
  } catch (error) {
    runtime.error = String(error.message || error).slice(0, 160);
    toast(`Cảnh ${index + 1} VIDEO: ${runtime.error}`, true);
  } finally {
    runtime.busy = false;
    rerenderScene(index);
    updateProgress();
    saveState();
  }
}

// Sản xuất tuần tự toàn bộ: từng cảnh START → END → VIDEO rồi mới sang cảnh sau.
async function runAllScenes() {
  if (!state.storyboard) { toast('Chưa có kịch bản — bấm "Lên kịch bản" trước.', true); return; }
  if (state.running) return;
  state.running = true;
  $('run-all').disabled = true;
  $('run-all').textContent = 'Đang sản xuất…';
  try {
    for (let index = 0; index < state.storyboard.scenes.length; index += 1) {
      const runtime = state.scenes[index];
      if (state.config.autoChain && index > 0 && state.scenes[index - 1].endItem) {
        runtime.startItem = state.scenes[index - 1].endItem;
        rerenderScene(index);
      } else if (!runtime.startItem) {
        await runSlot(index, 'start');
      }
      if (runtime.error) continue; // lỗi START thì bỏ qua video cảnh này
      if (!runtime.endItem) await runSlot(index, 'end');
      if (runtime.error) continue;
      if (!runtime.videoItem) await runScene(index);
    }
    toast('Hoàn tất sản xuất toàn bộ kịch bản.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.running = false;
    $('run-all').disabled = false;
    $('run-all').textContent = '▶ Sản xuất toàn bộ';
    updateProgress();
    saveState();
  }
}

// ── Nhân vật ────────────────────────────────────────────────────────────

async function addCharacterFromPicker() {
  const pick = $('character-pick');
  const id = pick.value;
  if (!id) { toast('Chọn ảnh trong danh sách trước.', true); return; }
  if (state.characters.length >= 2) { toast('Tối đa 2 ảnh nhân vật.', true); return; }
  state.characters.push(id);
  saveState();
  renderCharacters();
}

async function addCharacterFromFile(file) {
  if (state.characters.length >= 2) { toast('Tối đa 2 ảnh nhân vật.', true); return; }
  if (!file) return;
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('Đọc file thất bại.'));
    reader.readAsDataURL(file);
  });
  try {
    const response = await requestOr401(`/playground/api/projects/${state.projectId}/items`, {
      method: 'POST',
      body: JSON.stringify({ name: `Character ${new Date().toLocaleTimeString()}`, image: dataUrl }),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || 'Upload ảnh nhân vật thất bại.');
    }
    const project = await response.json();
    const added = (project.results || []).slice(-1)[0];
    if (added) {
      state.characters.push(String(added.id));
      trackItem(added);
    }
    saveState();
    renderCharacters();
    toast('Đã thêm ảnh nhân vật.');
  } catch (error) {
    toast(error.message, true);
  }
}

// ── Progress ────────────────────────────────────────────────────────────

function updateProgress() {
  if (!state.storyboard) {
    els.progress.hidden = true;
    return;
  }
  const frames = state.scenes.filter((s) => s.startItem && s.endItem).length;
  const videos = state.scenes.filter((s) => s.videoItem).length;
  els.progress.hidden = false;
  els.progress.textContent = `Frame ${frames}/${state.scenes.length} · Video ${videos}/${state.scenes.length}`;
  const runAll = $('run-all');
  if (runAll) runAll.disabled = state.running;
}

// ── Boot ────────────────────────────────────────────────────────────────

// Chống phải chuột trên canvas/thẻ — trong ô nhập (topic/prompt) vẫn giữ
// menu phải để copy/paste bình thường.
document.addEventListener('contextmenu', (event) => {
  if (event.target.closest('input, textarea')) return;
  event.preventDefault();
});
// Chặn selectstart phòng engine không ăn user-select:none (loại trừ ô nhập).
document.addEventListener('selectstart', (event) => {
  if (!event.target.closest('input, textarea')) event.preventDefault();
});

// Lỗi script không được nuốt im lặng — hiện toast để thấy ngay khi UI gãy.
window.addEventListener('error', (event) => {
  toast(`Lỗi script: ${event.message}`, true);
});
window.addEventListener('unhandledrejection', (event) => {
  toast(`Lỗi async: ${event.reason?.message || event.reason}`, true);
});

els.gateRetry.addEventListener('click', boot);
wireCanvasControls();
boot();
