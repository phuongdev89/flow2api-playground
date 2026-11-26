// Video Studio Flow — port vanilla JS từ tool CINEMAFLOW trên labs.google Flow.
// Orchestrator thuần frontend: gọi lại pipeline generate có sẵn của playground
// (job nền + refs + watermark), tự lo planner/queue/chain END→START.

const LS_KEY = 'studio_state_v1';

const $ = (id) => document.getElementById(id);

const els = {
  gate: $('gate'),
  app: $('app'),
  topic: $('topic'),
  sceneCount: $('scene-count'),
  sceneCountOut: $('scene-count-out'),
  planButton: $('plan-button'),
  importButton: $('import-button'),
  exportButton: $('export-button'),
  resetButton: $('reset-button'),
  importFile: $('import-file'),
  imageModel: $('image-model'),
  imageRes: $('image-res'),
  videoModel: $('video-model'),
  videoModelHint: $('video-model-hint'),
  characterList: $('character-list'),
  characterPick: $('character-pick'),
  characterAdd: $('character-add'),
  characterFile: $('character-file'),
  autoChain: $('auto-chain'),
  autoRun: $('auto-run'),
  runAll: $('run-all'),
  progress: $('progress'),
  board: $('board'),
  boardTitle: $('board-title'),
  boardSub: $('board-sub'),
  toast: $('toast'),
  plannerBadge: $('planner-badge'),
  gateRetry: $('gate-retry'),
};

// ── State ────────────────────────────────────────────────────────────────

const state = {
  projectId: null,
  projectName: '',
  storyboard: null, // {topic, category, scenes[], ...} schema tool gốc
  scenes: [],       // runtime per-scene: {startItem, endItem, videoItem, busy, error}
  characters: [],   // tối đa 2 item id (role=reference)
  imageModels: [],
  videoModels: [],
  config: {
    imageModel: '',
    imageRes: '1K',
    videoModel: 'omni-1.2-flash',
    autoChain: true,
    autoRun: false,
  },
  running: false,
};

function saveState() {
  const data = {
    projectId: state.projectId,
    storyboard: state.storyboard,
    scenes: state.scenes.map(({ startItem, endItem, videoItem }) => ({
      startItem, endItem, videoItem,
    })),
    characters: state.characters,
    config: state.config,
  };
  try { localStorage.setItem(LS_KEY, JSON.stringify(data)); } catch { /* đầy/quota — bỏ qua */ }
}

function loadState() {
  try {
    const raw = localStorage.getItem(LS_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    state.storyboard = data.storyboard || null;
    state.scenes = Array.isArray(data.scenes) ? data.scenes : [];
    state.characters = Array.isArray(data.characters) ? data.characters : [];
    Object.assign(state.config, data.config || {});
  } catch { /* state hỏng — bỏ qua */ }
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
    throw new Error('Phiên hết hạn — đăng nhập lại ở Playground.');
  }
  return response;
}

// ── Boot ────────────────────────────────────────────────────────────────

function showGate() {
  els.app.hidden = true;
  els.gate.hidden = false;
}

async function boot() {
  loadState();
  const session = await request('/playground/session');
  if (!session.ok) { showGate(); return; }
  els.gate.hidden = true;
  els.app.hidden = false;

  try {
    const wsResponse = await requestOr401('/playground/api/studio/workspace', { method: 'POST' });
    const workspace = await wsResponse.json();
    state.projectId = workspace.project.id;
    state.projectName = workspace.project.name;
    updatePlannerBadge(workspace.llm_ready);
    // Load lại project results (items có thể được tạo từ tab playground).
    const projectResponse = await requestOr401(`/playground/api/projects/${state.projectId}`);
    const project = await projectResponse.json();
    state._projectResults = project.results || [];
    indexItems(state._projectResults);
    reconcileStateWithProject(project.results || []);
  } catch (error) {
    toast(error.message, true);
    return;
  }

  // Đồng bộ UI với state đã load từ localStorage.
  els.autoChain.checked = state.config.autoChain;
  els.autoRun.checked = state.config.autoRun;
  if (state.storyboard) {
    els.topic.value = state.storyboard.topic || els.topic.value;
    els.boardTitle.textContent = `Cây gia phả: ${state.storyboard.topic}`;
    els.boardSub.textContent = `${state.storyboard.scenes.length} cảnh · tổng ~${state.storyboard.totalDurationSeconds}s · ${state.storyboard.dependencyExplanation}`;
  }

  await loadGenOptions();
  renderAll();
}

function updatePlannerBadge(llmReady) {
  els.plannerBadge.hidden = false;
  els.plannerBadge.textContent = llm_ready
    ? 'Planner: LLM (STUDIO_LLM_MODEL)'
    : 'Planner: engine mẫu (điền .env STUDIO_LLM_* để dùng LLM)';
  els.plannerBadge.classList.toggle('llm', llmReady);
  els.plannerBadge.classList.toggle('template', !llmReady);
}

// Item trong project có thể bị xoá từ tab playground — cắt tham chiếu chết.
function reconcileStateWithProject(results) {
  const byId = new Map(results.map((item) => [String(item.id), item]));
  state.characters = state.characters.filter((id) => byId.has(id));
  for (const scene of state.scenes) {
    for (const key of ['startItem', 'endItem', 'videoItem']) {
      const item = byId.get(scene[key]);
      if (scene[key] && !item) scene[key] = null;
    }
  }
  return byId;
}

async function loadGenOptions() {
  let options = null;
  try {
    const response = await requestOr401('/playground/api/gen-options');
    if (response.ok) options = await response.json();
  } catch { /* dùng fallback dưới */ }

  state.imageModels = options?.image?.models?.length
    ? options.image.models
    : [{ id: 'nano-banana-pro', label: 'Nano Banana Pro', base: 'nano-banana-pro', res: ['1K'] }];
  state.videoModels = options?.video?.models?.length
    ? options.video.models
    : [{ id: 'veo-3.1-lite', label: 'Veo 3.1 - Lite', length: [4, 6, 8], frames: true }];

  // Giữ lựa chọn cũ nếu endpoint vẫn cấp; default ưu tiên omni (giống tool).
  fillSelect(els.imageModel, state.imageModels.map((m) => ({ value: m.id, label: m.label })));
  if (!state.imageModels.some((m) => m.id === state.config.imageModel)) {
    const preferred = state.imageModels.find((m) => /flash-image$/.test(m.id))
      || state.imageModels.find((m) => !/lite/.test(m.id))
      || state.imageModels[0];
    state.config.imageModel = preferred?.id || '';
  }
  els.imageModel.value = state.config.imageModel;
  fillImageRes();

  fillSelect(els.videoModel, state.videoModels.map((m) => ({ value: m.id, label: m.label })));
  if (!state.videoModels.some((m) => m.id === state.config.videoModel)) {
    const omni = state.videoModels.find((m) => m.id.startsWith('omni-1.2'));
    state.config.videoModel = (omni || state.videoModels[0])?.id || '';
  }
  els.videoModel.value = state.config.videoModel;
  updateVideoModelHint();
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

function fillImageRes() {
  const model = state.imageModels.find((m) => m.id === state.config.imageModel);
  const res = model?.res?.length ? model.res : ['1K'];
  if (!res.includes(state.config.imageRes)) state.config.imageRes = res[0];
  fillSelect(els.imageRes, res.map((value) => ({ value, label: value })));
  els.imageRes.value = state.config.imageRes;
}

function videoModelSpec() {
  return state.videoModels.find((m) => m.id === state.config.videoModel) || null;
}

function updateVideoModelHint() {
  const spec = videoModelSpec();
  const lengths = spec?.length || [];
  els.videoModelHint.textContent = spec
    ? `Thời lượng hỗ trợ: ${lengths.length ? lengths.join('s · ') + 's' : 'mặc định engine'} — mỗi cảnh tuỳ chỉnh riêng.`
    : '';
}

// ── Planner ─────────────────────────────────────────────────────────────

async function plan() {
  const topic = els.topic.value.trim();
  if (!topic) { toast('Nhập chủ đề trước đã.', true); return; }
  const sceneCount = Number(els.sceneCount.value);
  els.planButton.disabled = true;
  els.planButton.textContent = 'Đang lên kịch bản…';
  try {
    const response = await requestOr401('/playground/api/studio/plan', {
      method: 'POST',
      body: JSON.stringify({ topic, scene_count: sceneCount }),
    });
    const data = await response.json();
    if (!response.ok) { toast(data.detail || data.error || 'Lên kịch bản thất bại.', true); return; }
    applyStoryboard(data.storyboard);
    toast(`Đã lên ${data.storyboard.scenes.length} cảnh (${data.source === 'llm' ? 'LLM' : 'engine mẫu'}).`);
  } catch (error) {
    toast(error.message, true);
  } finally {
    els.planButton.disabled = false;
    els.planButton.textContent = '✨ Lên kịch bản (Fan out N cảnh)';
  }
}

function applyStoryboard(storyboard) {
  state.storyboard = storyboard;
  state.scenes = storyboard.scenes.map(() => ({
    startItem: null, endItem: null, videoItem: null, busy: false, error: null,
  }));
  // Plan duration của model hiện tại không hỗ trợ thì clamp về gần nhất.
  const lengths = videoModelSpec()?.length || [];
  if (lengths.length) {
    for (const scene of storyboard.scenes) {
      if (!lengths.includes(scene.durationSeconds)) {
        scene.durationSeconds = nearestLength(lengths, scene.durationSeconds);
      }
    }
  }
  els.boardTitle.textContent = `Cây gia phả: ${storyboard.topic}`;
  els.boardSub.textContent = `${storyboard.scenes.length} cảnh · tổng ~${storyboard.totalDurationSeconds}s · ${storyboard.dependencyExplanation}`;
  renderAll();
  saveState();
}

function nearestLength(lengths, wanted) {
  return lengths.reduce((best, value) => (
    Math.abs(value - wanted) < Math.abs(best - wanted) ? value : best
  ), lengths[0]);
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
    els.topic.value = data.storyboard.topic || '';
    toast(`Import xong ${data.storyboard.scenes.length} cảnh.`);
  } catch (error) {
    toast(error.message, true);
  }
}

// ── Render board ────────────────────────────────────────────────────────

const ROLE_LABEL = { hook: 'Hook', setup: 'Setup', body: 'Body', reward: 'Reward' };

function renderAll() {
  renderCharacters();
  renderBoard();
  updateProgress();
}

function renderCharacters() {
  els.characterList.innerHTML = '';
  for (const id of state.characters) {
    const item = lookupItem(id);
    const li = document.createElement('li');
    li.title = item?.name || id;
    const img = document.createElement('img');
    img.src = itemUrl(item);
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
    li.append(img, remove);
    els.characterList.appendChild(li);
  }
  if (state.projectId) refreshCharacterPicker();
}

async function refreshCharacterPicker() {
  try {
    const response = await requestOr401(`/playground/api/projects/${state.projectId}`);
    const project = await response.json();
    const candidates = (project.results || []).filter(
      (item) => item.kind === 'image' && !state.characters.includes(String(item.id)),
    );
    fillSelect(
      els.characterPick,
      [{ value: '', label: '— Chọn ảnh từ project —' }].concat(
        candidates.slice(-40).reverse().map((item) => ({
          value: String(item.id),
          label: `${item.name || 'ảnh'} ${item.meta?.source === 'playground-reference' ? '(ref)' : ''}`.trim(),
        })),
      ),
    );
  } catch { /* poll lần sau */ }
}

function itemUrl(item) {
  return item?.url || '';
}

function lookupItem(id) {
  return state._items?.get(String(id)) || null;
}

function indexItems(results) {
  state._items = new Map((results || []).map((item) => [String(item.id), item]));
}

function renderBoard() {
  indexItems(state._projectResults || []);
  els.board.innerHTML = '';
  if (!state.storyboard) return;

  state.storyboard.scenes.forEach((plan, index) => {
    const runtime = state.scenes[index] || (state.scenes[index] = {
      startItem: null, endItem: null, videoItem: null, busy: false, error: null,
    });
    els.board.appendChild(buildSceneCard(plan, index, runtime));
  });
}

function buildSceneCard(plan, index, runtime) {
  const card = document.createElement('section');
  card.className = 'scene';
  card.dataset.index = String(index);
  refreshSceneClass(card, runtime);

  // ── header ──
  const header = document.createElement('header');
  header.className = 'scene-header';
  const id = document.createElement('span');
  id.className = 'scene-id';
  id.textContent = `S${String(index + 1).padStart(2, '0')}`;
  const name = document.createElement('span');
  name.className = 'scene-name';
  name.textContent = plan.nameVi;
  const role = document.createElement('span');
  role.className = `role-badge ${plan.role}`;
  role.textContent = ROLE_LABEL[plan.role] || plan.role;

  const lengths = videoModelSpec()?.length || [];
  const duration = document.createElement('select');
  duration.setAttribute('aria-label', 'Thời lượng cảnh');
  if (lengths.length) {
    fillSelect(duration, lengths.map((value) => ({ value: String(value), label: `${value}s` })));
    duration.value = String(nearestLength(lengths, plan.durationSeconds));
  } else {
    fillSelect(duration, [String(plan.durationSeconds)].map((value) => ({ value, label: `${value}s` })));
    duration.value = String(plan.durationSeconds);
  }
  duration.addEventListener('change', () => {
    plan.durationSeconds = Number(duration.value);
    saveState();
  });

  header.append(id, name, role, duration);

  // ── body: 3 slots ──
  const body = document.createElement('div');
  body.className = 'scene-body';
  body.appendChild(buildSlot(plan, index, runtime, 'start', 'START', card));
  body.appendChild(buildSlot(plan, index, runtime, 'end', 'END', card));
  body.appendChild(buildSlot(plan, index, runtime, 'video', 'VIDEO', card));

  // ── prompts ──
  const details = document.createElement('details');
  details.className = 'scene-prompts';
  const summary = document.createElement('summary');
  summary.textContent = 'Prompt (sửa trực tiếp)';
  const grid = document.createElement('div');
  grid.className = 'prompt-grid';
  for (const key of ['startPrompt', 'endPrompt', 'videoPrompt']) {
    const label = document.createElement('label');
    label.textContent = key.replace('Prompt', '');
    const textarea = document.createElement('textarea');
    textarea.value = plan[key] || '';
    textarea.addEventListener('change', () => {
      plan[key] = textarea.value.trim();
      saveState();
    });
    label.appendChild(textarea);
    grid.appendChild(label);
  }
  details.append(summary, grid);

  card.append(header, body, details);
  return card;
}

function refreshSceneClass(card, runtime) {
  card.classList.toggle('is-busy', Boolean(runtime.busy));
  card.classList.toggle('is-error', Boolean(runtime.error));
  card.classList.toggle('is-done-video', Boolean(runtime.videoItem));
}

function buildSlot(plan, index, runtime, slot, label, card) {
  const figure = document.createElement('figure');
  figure.className = 'slot';

  const caption = document.createElement('figcaption');
  const captionText = document.createElement('span');
  captionText.textContent = label;
  caption.appendChild(captionText);

  const media = document.createElement('button');
  media.type = 'button';
  media.className = 'slot-media';
  media.setAttribute('aria-label', `Xem ${label} cảnh ${index + 1}`);

  const status = document.createElement('div');
  status.className = 'slot-status';

  const actions = document.createElement('div');
  actions.className = 'slot-actions';

  const refresh = () => {
    const itemId = runtime[`${slot}Item`];
    const item = lookupItem(itemId);
    media.innerHTML = '';
    const kind = slot === 'video' ? 'video' : 'image';
    if (item && item.kind === kind) {
      if (kind === 'image') {
        const img = document.createElement('img');
        img.src = itemUrl(item);
        img.alt = `${label} cảnh ${index + 1}`;
        media.appendChild(img);
      } else {
        media.textContent = '▶ Video xong';
      }
    } else {
      media.textContent = itemId ? '…mất' : 'trống';
    }
    // Auto-chain: START của cảnh sau chính là END cảnh trước.
    caption.querySelector('.chip')?.remove();
    const chained = slot === 'start' && state.config.autoChain && index > 0
      && runtime.startItem
      && runtime.startItem === state.scenes[index - 1]?.endItem;
    media.classList.toggle('reused', Boolean(chained));
    if (chained) {
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = 'Tái dùng END trước';
      caption.appendChild(chip);
    }

    status.className = 'slot-status';
    if (runtime.busy && !runtime[`${slot}Item`]) {
      status.textContent = 'đang tạo…';
      status.classList.add('busy');
    } else if (runtime.error) {
      status.textContent = runtime.error;
      status.classList.add('error');
    } else if (runtime[`${slot}Item`]) {
      status.textContent = 'xong';
      status.classList.add('ok');
    }

    actions.innerHTML = '';
    if (slot === 'video') {
      const render = document.createElement('button');
      render.type = 'button';
      render.textContent = 'Render';
      render.disabled = !(runtime.startItem && runtime.endItem) || state.running;
      render.addEventListener('click', () => runScene(index, card));
      actions.appendChild(render);
    } else if (!(slot === 'start' && state.config.autoChain && index > 0 && state.scenes[index - 1]?.endItem)) {
      const gen = document.createElement('button');
      gen.type = 'button';
      gen.textContent = runtime[`${slot}Item`] ? 'Tạo lại' : 'Tạo';
      gen.disabled = state.running;
      gen.addEventListener('click', () => runSlot(index, slot, card));
      actions.appendChild(gen);
    }
  };

  refresh();
  // Re-render slot khi state scene đổi — rẻ và đủ dùng cho board < 25 cảnh.
  runtime._listeners = runtime._listeners || {};
  runtime._listeners[slot] = refresh;
  media.addEventListener('click', () => {
    const item = lookupItem(runtime[`${slot}Item`]);
    if (item?.url) window.open(item.url, '_blank');
  });

  figure.append(caption, media, status, actions);
  return figure;
}

function rerenderScene(index) {
  const card = els.board.querySelector(`.scene[data-index="${index}"]`);
  const runtime = state.scenes[index];
  if (!card || !runtime) return;
  refreshSceneClass(card, runtime);
  for (const slot of ['start', 'end', 'video']) {
    runtime._listeners?.[slot]?.();
  }
}

function updateProgress() {
  if (!state.storyboard) {
    els.progress.textContent = '';
    return;
  }
  const frames = state.scenes.filter((s) => s.startItem && s.endItem).length;
  const videos = state.scenes.filter((s) => s.videoItem).length;
  els.progress.textContent = `Cảnh đủ frame: ${frames}/${state.scenes.length} · video: ${videos}/${state.scenes.length}`;
  els.runAll.disabled = state.running;
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

async function runSlot(index, slot, card) {
  const runtime = state.scenes[index];
  const plan = state.storyboard.scenes[index];
  if (!runtime || !plan) return;

  // Chain END→START: cảnh sau lấy ảnh END của cảnh trước làm START.
  if (slot === 'start' && state.config.autoChain && index > 0 && state.scenes[index - 1].endItem) {
    runtime.startItem = state.scenes[index - 1].endItem;
    saveState();
    rerenderScene(index);
    return;
  }

  const model = state.imageModels.find((m) => m.id === state.config.imageModel);
  if (!model) { toast('Chưa chọn được model ảnh.', true); return; }

  const prompt = slot === 'start' ? plan.startPrompt : plan.endPrompt;
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
      res: els.imageRes.value,
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
    if (card) refreshSceneClass(card, runtime);
  }
}

async function runScene(index, card) {
  const runtime = state.scenes[index];
  const plan = state.storyboard.scenes[index];
  if (!runtime?.startItem || !runtime?.endItem) {
    toast('Cần đủ START + END trước khi render video.', true);
    return;
  }
  const spec = videoModelSpec();
  if (!spec) { toast('Chưa chọn được model video.', true); return; }

  const lengths = spec.length || [];
  const duration = lengths.length ? nearestLength(lengths, plan.durationSeconds) : plan.durationSeconds;
  plan.durationSeconds = duration;

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
      prompt: plan.videoPrompt || plan.stateChange || plan.nameVi,
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

function trackItem(item) {
  if (!state._items) state._items = new Map();
  state._items.set(String(item.id), item);
  if (!Array.isArray(state._projectResults)) state._projectResults = [];
  state._projectResults.push(item);
}

// Sản xuất tuần tự toàn bộ: từng cảnh START → END → VIDEO rồi mới sang cảnh sau
// (đúng tinh thần "Tự động gen ảnh tuần tự" của tool gốc).
async function runAllScenes() {
  if (!state.storyboard) { toast('Chưa có kịch bản — bấm "Lên kịch bản" trước.', true); return; }
  if (state.running) return;
  state.running = true;
  els.runAll.disabled = true;
  els.runAll.textContent = 'Đang sản xuất…';
  try {
    for (let index = 0; index < state.storyboard.scenes.length; index += 1) {
      const runtime = state.scenes[index];
      const plan = state.storyboard.scenes[index];
      const card = els.board.querySelector(`.scene[data-index="${index}"]`);
      // START: chain từ END cảnh trước nếu bật auto-chain.
      if (!(state.config.autoChain && index > 0 && state.scenes[index - 1].endItem)
        && !runtime.startItem) {
        await runSlot(index, 'start', card);
      } else if (state.config.autoChain && index > 0 && state.scenes[index - 1].endItem) {
        runtime.startItem = state.scenes[index - 1].endItem;
        rerenderScene(index);
      }
      if (runtime.error) continue; // lỗi START thì bỏ qua video cảnh này
      if (!runtime.endItem) await runSlot(index, 'end', card);
      if (runtime.error) continue;
      if (!runtime.videoItem) await runScene(index, card);
    }
    toast('Hoàn tất sản xuất toàn bộ kịch bản.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    state.running = false;
    els.runAll.disabled = false;
    els.runAll.textContent = '▶ Sản xuất toàn bộ';
    updateProgress();
    saveState();
  }
}

// ── Nhân vật ────────────────────────────────────────────────────────────

async function addCharacterFromPicker() {
  const id = els.characterPick.value;
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

// ── Reset / wiring ──────────────────────────────────────────────────────

function resetBoard() {
  if (!confirm('Xoá kịch bản hiện tại? (Ảnh/video trong project giữ nguyên)')) return;
  state.storyboard = null;
  state.scenes = [];
  els.boardTitle.textContent = 'Cây gia phả sản xuất chưa khởi tạo';
  els.boardSub.textContent = 'Nhập chủ đề rồi bấm "Lên kịch bản" — START → END → Video, đọc dọc từng cảnh.';
  renderBoard();
  updateProgress();
  saveState();
}

function wire() {
  els.sceneCount.addEventListener('input', () => {
    els.sceneCountOut.textContent = els.sceneCount.value;
  });
  els.planButton.addEventListener('click', plan);
  els.exportButton.addEventListener('click', exportJson);
  els.importButton.addEventListener('click', () => els.importFile.click());
  els.importFile.addEventListener('change', () => {
    const file = els.importFile.files?.[0];
    els.importFile.value = '';
    if (file) importJson(file);
  });
  els.resetButton.addEventListener('click', resetBoard);

  els.imageModel.addEventListener('change', () => {
    state.config.imageModel = els.imageModel.value;
    fillImageRes();
    saveState();
  });
  els.imageRes.addEventListener('change', () => {
    state.config.imageRes = els.imageRes.value;
    saveState();
  });
  els.videoModel.addEventListener('change', () => {
    state.config.videoModel = els.videoModel.value;
    updateVideoModelHint();
    renderBoard(); // duration options đổi theo model
    updateProgress();
    saveState();
  });

  els.characterAdd.addEventListener('click', addCharacterFromPicker);
  els.characterFile.addEventListener('change', () => {
    const file = els.characterFile.files?.[0];
    els.characterFile.value = '';
    if (file) addCharacterFromFile(file);
  });

  els.autoChain.addEventListener('change', () => {
    state.config.autoChain = els.autoChain.checked;
    renderBoard();
    saveState();
  });
  els.autoRun.addEventListener('change', () => {
    state.config.autoRun = els.autoRun.checked;
    saveState();
  });
  els.runAll.addEventListener('click', runAllScenes);
  els.gateRetry.addEventListener('click', boot);
}

wire();
boot();
