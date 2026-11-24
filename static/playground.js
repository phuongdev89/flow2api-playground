const login = document.querySelector('#login');
const form = document.querySelector('#login-form');
const urlInput = document.querySelector('#api-url');
const keyInput = document.querySelector('#api-key');
const loginError = document.querySelector('#login-error');
const loginButton = document.querySelector('#login-button');
const workspace = document.querySelector('#workspace');
const logout = document.querySelector('#logout');
const wmSettingsButton = document.querySelector('#wm-settings');
const densityPicker = document.querySelector('.density-picker');
const prompt = document.querySelector('#prompt');
const projectList = document.querySelector('#project-list');
const emptyProjects = document.querySelector('#empty-projects');
const newProject = document.querySelector('#new-project');
const sidebarToggle = document.querySelector('#sidebar-toggle');
const densityOptions = [...document.querySelectorAll('.density-option')];
const sendButton = document.querySelector('#send');
const genTrigger = document.querySelector('#gen-trigger');
const genLabel = document.querySelector('#gen-label');
const genIconImage = document.querySelector('#gen-icon-image');
const genIconVideo = document.querySelector('#gen-icon-video');
const genDialog = document.querySelector('#gen-dialog');
const genTabs = [...document.querySelectorAll('.gen-tab')];
const genBodies = {
  image: document.querySelector('#gen-body-image'),
  video: document.querySelector('#gen-body-video'),
};
const genRatioRows = {
  image: document.querySelector('#gen-ratios-image'),
  video: document.querySelector('#gen-ratios-video'),
};
const genQtyRows = {
  image: document.querySelector('#gen-qty-image'),
  video: document.querySelector('#gen-qty-video'),
};
const genModelButtons = {
  image: document.querySelector('#gen-model-image'),
  video: document.querySelector('#gen-model-video'),
};
const genVmodeRow = document.querySelector('#gen-vmode');
const genResRow = document.querySelector('#gen-res');
const genLengthRow = document.querySelector('#gen-length');
const genLengthValue = document.querySelector('#gen-length-value');
const genLengthSlider = document.querySelector('#gen-length-slider');
const genLengthTicks = document.querySelector('#gen-length-ticks');
const genModelList = document.querySelector('#gen-model-list');
const genResImageRow = document.querySelector('#gen-res-image');
const composer = document.querySelector('.composer');
const composerRefs = document.querySelector('#composer-refs');
const composerFrames = document.querySelector('#composer-frames');
const framesExit = document.querySelector('#frames-exit');
const framesSwap = document.querySelector('#frames-swap');
const frameSlotEls = {
  start: document.querySelector('#frame-slot-start'),
  end: document.querySelector('#frame-slot-end'),
};
const framePicker = document.querySelector('#frame-picker');
const framePickerTitle = document.querySelector('#frame-picker-title');
const framePickerGrid = document.querySelector('#frame-picker-grid');
const framePickerEmpty = document.querySelector('#frame-picker-empty');
const framePickerClose = document.querySelector('#frame-picker-close');
const frameUploadInput = document.querySelector('#frame-upload-input');
const refUploadInput = document.querySelector('#ref-upload-input');
const stageDropOverlay = document.querySelector('#stage-drop-overlay');
const resultsGrid = document.querySelector('#results-grid');
const resultsEmpty = document.querySelector('#results-empty');
const selectionToolbar = document.querySelector('#selection-toolbar');
const selectionCount = document.querySelector('#selection-count');
const selectionClear = document.querySelector('#selection-clear');
const selectionDelete = document.querySelector('#selection-delete');

let projects = [];
let activeProject = null;
let openActions = null;
let resultColumns = 10;
let promptSaveTimer;
let toastTimer;
// Generation jobs live server-side; jobsById theo dõi TỪNG job riêng lẻ — mỗi
// lần gửi là một job độc lập xếp hàng, composer không bao giờ khoá. Job đang
// POST (chưa có job_id từ server) dùng khoá tạm "tmp:*". Queue được mirror vào
// localStorage để sống sót qua F5 (reconcile theo GET /projects/{id}/jobs).
const jobsById = new Map(); // job id -> { projectId, jobId, mode, ratio, count, startedAt, partial }
const timersByJob = new Map(); // job id -> poll interval handle
let tmpJobSeq = 0; // đánh số khoá tạm cho job đang POST
const jobsFetchInFlight = new Map(); // project id -> Promise GET /jobs đang bay (dedupe poller)
const COLUMNS_STORAGE_KEY = 'playground-columns';
const JOB_QUEUE_KEY = 'playground_job_queue';

// ── Selection (bôi chọn tham chiếu trong stage) ─────────────────────────
// Chọn = tham chiếu: thứ tự chọn quyết định chip trong composer; role
// 'reference' trên server được sync ngầm để giữ selection qua reload.
const MAX_REFS = { image: 10, video: 3 };
const selectionByProject = new Map(); // project id -> [item id theo thứ tự chọn]
const frameSlotsByProject = new Map(); // project id -> { startId, endId } cho chế độ Frames
const roleSyncTimers = new Map(); // project id -> debounce handle
let vmodeManual = false;
let framePickerSlot = null; // 'start' | 'end' | null

const GEN_OPTIONS = {
  image: {
    ratios: ['16:9', '4:3', '1:1', '3:4', '9:16'],
    // 4K bị bỏ cho ảnh (mô phỏng Flow): chỉ 1K/2K.
    models: [{ id: 'nano-banana-pro', label: 'Nano Banana Pro', icon: '🍌', res: ['1K', '2K'], refs: true }],
  },
  video: {
    ratios: ['16:9', '9:16'],
    models: [
      { id: 'veo-3.1-lite', label: 'Veo 3.1 - Lite', refs: true, frames: true },
      { id: 'veo-3.1-fast', label: 'Veo 3.1 - Fast', res: ['720p', '1080p', '4K'], length: [4, 8], refs: true, frames: false },
      { id: 'veo-3.1-fast-ultra', label: 'Veo 3.1 Fast Ultra', res: ['720p', '1080p', '4K'], length: [4, 8], refs: true, frames: false },
      { id: 'veo-3.1-quality', label: 'Veo 3.1 - Quality', res: ['720p', '1080p', '4K'], length: [4, 8], refs: false, frames: true },
    ],
  },
};
let GEN_CAPS = null; // merged /playground/api/gen-options capability; static GEN_OPTIONS until fetched
const genState = {
  mode: 'image',
  image: { model: 'nano-banana-pro', ratio: '9:16', qty: 1, res: '1K' },
  video: { model: 'veo-3.1-lite', vmode: 'text', ratio: '9:16', qty: 1, res: '720p', length: 8 },
};
let genModelListOpen = false;

async function request(path, options = {}) {
  const headers = {
    'X-Playground-Request': '1',
    ...(options.body ? { 'Content-Type': 'application/json' } : {}),
    ...options.headers,
  };
  return fetch(path, {
    ...options,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
  });
}

function projectPath(id, suffix = '') {
  return `/playground/api/projects/${encodeURIComponent(id)}${suffix}`;
}

function parseProject(value) {
  if (!value || typeof value.id !== 'string' || typeof value.name !== 'string') return null;
  let items = value.results ?? [];
  if (typeof items === 'string') {
    try { items = JSON.parse(items); } catch { items = []; }
  }
  if (!Array.isArray(items)) items = [];
  return {
    ...value,
    name: value.name.trim().slice(0, 120) || 'Project',
    prompt: typeof value.prompt === 'string' ? value.prompt : '',
    results: items,
  };
}

function sortProjects() {
  projects.sort((a, b) => {
    const left = Date.parse(a.updated_at) || Number(a.updated_at) || 0;
    const right = Date.parse(b.updated_at) || Number(b.updated_at) || 0;
    return right - left;
  });
}

function showToast(message) {
  let status = document.querySelector('#composer-status');
  if (!status) {
    status = document.createElement('p');
    status.id = 'composer-status';
    status.className = 'composer-status';
    status.setAttribute('role', 'status');
    document.querySelector('.composer').append(status);
  }
  status.textContent = message;
  status.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { status.hidden = true; }, 2600);
}

function closeProjectActions(restoreFocus = false) {
  if (!openActions) return;
  const { toggle, actions } = openActions;
  actions.hidden = true;
  toggle.setAttribute('aria-expanded', 'false');
  openActions = null;
  if (restoreFocus) toggle.focus();
}

function makeButton(className, label) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  button.textContent = label;
  return button;
}

function renderProjects(focusProject, focusMore = false) {
  closeProjectActions();
  let focusTarget;
  const rows = projects.map((project, index) => {
    const row = document.createElement('li');
    row.className = 'project-row';
    row.dataset.projectId = project.id;
    const select = makeButton('project-select', project.name);
    select.title = project.name;
    if (project.id === activeProject) select.setAttribute('aria-current', 'true');
    const more = makeButton('project-more', '…');
    more.setAttribute('aria-label', `Tùy chọn: ${project.name}`);
    more.setAttribute('aria-expanded', 'false');
    const actions = document.createElement('div');
    actions.className = 'project-actions';
    actions.id = `project-actions-${index}`;
    actions.hidden = true;
    more.setAttribute('aria-controls', actions.id);
    const rename = makeButton('project-action', 'Rename');
    const deleteButton = makeButton('project-action project-action-danger', 'Delete');
    actions.append(rename, deleteButton);
    row.append(select, more, actions);

    select.addEventListener('click', async () => {
      activeProject = project.id;
      exitExtendMode(); // nối tiếp gắn với project cũ — đổi project là hủy
      renderProjects(project.id);
      showProjectContents();
      try {
        const response = await request(projectPath(project.id, '/activity'), { method: 'POST' });
        if (!response.ok) return;
        const updated = parseProject(await response.json());
        if (updated) {
          projects = [updated, ...projects.filter(({ id }) => id !== updated.id)];
          sortProjects();
          renderProjects(updated.id);
        }
      } catch {
        showToast('Project đang chờ kết nối máy chủ.');
      }
    });

    more.addEventListener('click', () => {
      const alreadyOpen = openActions?.toggle === more;
      closeProjectActions();
      if (alreadyOpen) return;
      openActions = { toggle: more, actions };
      actions.hidden = false;
      more.setAttribute('aria-expanded', 'true');
    });

    rename.addEventListener('click', async () => {
      closeProjectActions(true);
      const name = window.prompt('Đổi tên project:', project.name)?.trim().slice(0, 120);
      if (!name || name === project.name) return;
      try {
        const response = await request(projectPath(project.id), {
          method: 'PATCH',
          body: JSON.stringify({ name }),
        });
        if (!response.ok) throw new Error('Project update failed');
        const updated = parseProject(await response.json());
        if (updated) projects = [updated, ...projects.filter(({ id }) => id !== updated.id)];
        sortProjects();
        renderProjects(project.id, true);
      } catch {
        showToast('Chưa thể đổi tên project.');
      }
    });

    deleteButton.addEventListener('click', async () => {
      closeProjectActions(true);
      if (!window.confirm(`Xóa project “${project.name}”?`)) return;
      try {
        const response = await request(projectPath(project.id), { method: 'DELETE' });
        if (!response.ok) throw new Error('Project delete failed');
      projects = projects.filter(({ id }) => id !== project.id);
      clearProjectJobs(project.id);
      if (activeProject === project.id) activeProject = projects[0]?.id ?? null;
      renderProjects(activeProject);
        showProjectContents();
        if (!activeProject) newProject.focus();
      } catch {
        showToast('Chưa thể xóa project.');
      }
    });
    if (project.id === focusProject) focusTarget = focusMore ? more : select;
    return row;
  });
  projectList.replaceChildren(...rows);
  emptyProjects.hidden = rows.length > 0;
  focusTarget?.focus();
}

function projectRunningCount(projectId) {
  // Số job đang chạy của một project (gồm cả POST chưa trả job_id). Chỉ dùng để
  // chặn bulk delete đua với append_results server-side — send KHÔNG bị chặn.
  // Job lỗi đã dừng hẳn (append không bao giờ xảy ra nữa) → không tính.
  if (!projectId) return 0;
  let count = 0;
  for (const job of jobsById.values()) {
    if (job.projectId === projectId && job.status !== 'error') count += 1;
  }
  return count;
}

function syncComposerState() {
  // Composer không bao giờ khoá vì job đang chạy — mỗi lần gửi là một job mới.
  sendButton.disabled = !prompt.value.trim();
}

function showProjectContents() {
  const active = projects.find(({ id }) => id === activeProject);
  prompt.value = active?.prompt ?? '';
  autoGrowPrompt();
  syncComposerState();
  resumePollingIfNeeded(activeProject);
  reconcileProjectJobs(activeProject);
  // Selection khôi phục từ role đã sync server (reload giữ nguyên lựa chọn).
  selectionByProject.set(
    activeProject,
    projectReferenceItems(active).map((item) => String(item.id)),
  );
  syncFrameSlotsFromSelection(activeProject);
  closeFramePicker();
  renderResultsGrid();
  renderComposerRefs();
  syncSelectionToolbar();
}

// ── Selection helpers ────────────────────────────────────────────────────

function selectionIds(projectId = activeProject) {
  return selectionByProject.get(projectId) ?? [];
}

function isSelected(itemId) {
  return selectionIds().includes(String(itemId));
}

function selectedItems() {
  const project = activeProjectData();
  if (!project) return [];
  const byId = new Map((project.results ?? []).map((item) => [String(item.id), item]));
  return selectionIds()
    .map((id) => byId.get(id))
    .filter(Boolean);
}

function selectedImageItems() {
  return selectedItems().filter((item) => item.kind === 'image');
}

function isFramesMode() {
  return genState.mode === 'video' && genState.video.vmode === 'frames';
}

function projectImageById(projectId = activeProject) {
  const project = projects.find(({ id }) => id === projectId);
  const byId = new Map();
  for (const item of project?.results ?? []) {
    if (item && item.kind === 'image' && item.id !== undefined && item.id !== null) {
      byId.set(String(item.id), item);
    }
  }
  return byId;
}

function getFrameSlots(projectId = activeProject) {
  if (!projectId) return { startId: null, endId: null };
  const byId = projectImageById(projectId);
  const stored = frameSlotsByProject.get(projectId);
  if (!stored) {
    const imgs = (selectionByProject.get(projectId) ?? []).filter((id) => byId.has(id));
    return { startId: imgs[0] ?? null, endId: imgs[1] ?? null };
  }
  const startId = stored.startId && byId.has(stored.startId) ? stored.startId : null;
  const endId = stored.endId && byId.has(stored.endId) ? stored.endId : null;
  return { startId, endId };
}

function syncFrameSlotsFromSelection(projectId = activeProject) {
  if (!projectId) return;
  const byId = projectImageById(projectId);
  const imgs = (selectionByProject.get(projectId) ?? []).filter((id) => byId.has(id));
  const prev = frameSlotsByProject.get(projectId);
  if (imgs.length === 0) {
    frameSlotsByProject.set(projectId, { startId: null, endId: null });
    return;
  }
  if (imgs.length === 1) {
    if (prev && prev.startId === null && prev.endId === imgs[0]) {
      frameSlotsByProject.set(projectId, { startId: null, endId: imgs[0] });
    } else {
      frameSlotsByProject.set(projectId, { startId: imgs[0], endId: null });
    }
    return;
  }
  // Giữ thứ tự Start/End nếu 2 ảnh trùng khớp với slot hiện có (tránh nhảy ngược khi đồng bộ).
  if (prev && prev.startId && prev.endId && imgs.includes(prev.startId) && imgs.includes(prev.endId)) {
    frameSlotsByProject.set(projectId, { startId: prev.startId, endId: prev.endId });
    return;
  }
  frameSlotsByProject.set(projectId, { startId: imgs[0], endId: imgs[1] });
}

function selectionChanged({ preserveSlots = false } = {}) {
  if (!preserveSlots) syncFrameSlotsFromSelection();
  applyVideoVmodeAuto();
  renderResultsGrid();
  renderComposerRefs();
  renderGenDialog();
  syncSelectionRoles();
  syncSelectionToolbar();
}

// Auto-chọn vmode theo số ảnh đang chọn (giữ combo luôn hợp lệ):
// 0 ảnh → Text (nếu không phải user chủ động mở Frames); 1-2 ảnh → Frames; ≥3 → Ingredients.
function applyVideoVmodeAuto() {
  if (genState.mode !== 'video') return;
  if (genState.video.vmode === 'extend') return; // chế độ nối tiếp: không tự đổi
  const model = genSelectedModel('video');
  const count = selectedImageItems().length;
  if (count === 0) {
    if (!(genState.video.vmode === 'frames' && vmodeManual)) {
      genState.video.vmode = 'text';
    }
    return;
  }
  if (genState.video.vmode === 'text' && count > 0) {
    vmodeManual = false;
    genState.video.vmode = count <= 2 && model?.frames === true ? 'frames'
      : model?.refs === true ? 'ingredients' : 'text';
    return;
  }
  if (genState.video.vmode === 'frames' && count > 2) {
    vmodeManual = false;
    genState.video.vmode = model?.refs === true ? 'ingredients' : 'frames';
  }
}

function setSelected(itemId, selected) {
  const id = String(itemId);
  const current = selectionIds();
  const has = current.includes(id);
  if (selected && !has) {
    selectionByProject.set(activeProject, [...current, id]);
    selectionChanged();
  } else if (!selected && has) {
    selectionByProject.set(
      activeProject,
      current.filter((value) => value !== id),
    );
    selectionChanged();
  }
}

function clearSelection() {
  if (!selectionIds().length) return;
  selectionByProject.set(activeProject, []);
  selectionChanged();
}

// ── Toolbar xóa item đã chọn (marquee selection) ──────────────────────────
function syncSelectionToolbar() {
  if (!selectionToolbar) return;
  const count = selectionIds().length;
  selectionToolbar.hidden = count === 0;
  selectionCount.textContent = count ? `Đã chọn ${count} item` : '';
}

selectionClear.addEventListener('click', () => clearSelection());

selectionDelete.addEventListener('click', async () => {
  const project = activeProjectData();
  const items = selectedItems();
  if (!project || !items.length) return;
  // Đang có job chạy thì chặn: server đang append results, PATCH giờ sẽ lose data.
  if (projectRunningCount(project.id) > 0) {
    showToast('Đang có job tạo nội dung — đợi job xong rồi hãy xóa item.');
    return;
  }
  if (!window.confirm(`Xóa ${items.length} item đã chọn khỏi project?`)) return;
  const removeIds = new Set(items.map((item) => String(item.id)));
  const remaining = (project.results ?? []).filter((item) => !removeIds.has(String(item?.id)));
  try {
    // Xóa hàng loạt = gửi results đã lọc qua PATCH project (một request duy nhất).
    const response = await request(projectPath(project.id), {
      method: 'PATCH',
      body: JSON.stringify({ results: remaining }),
    });
    if (!response.ok) throw new Error('Bulk delete failed');
    const updated = replaceProjectWith(await response.json());
    if (updated) showToast(`Đã xóa ${items.length} item.`);
  } catch {
    showToast('Không xóa được các item đã chọn.');
  }
});

// Selection là nguồn sự thật cho role 'reference': sync ngầm (debounce) để
// reload trang giữ nguyên lựa chọn. Chỉ PATCH item thật sự đổi role.
function syncSelectionRoles() {
  const projectId = activeProject;
  const project = projects.find(({ id }) => id === projectId);
  if (!projectId || !project) return;
  const refIds = new Set(projectReferenceItems(project).map((item) => String(item.id)));
  const selectedImageIds = new Set(
    selectionIds(projectId).filter((id) => {
      const item = (project.results ?? []).find((candidate) => String(candidate?.id) === id);
      return item?.kind === 'image';
    }),
  );
  const pending = [
    ...[...selectedImageIds].filter((id) => !refIds.has(id)).map((id) => ({ id, role: 'reference' })),
    ...[...refIds].filter((id) => !selectedImageIds.has(id)).map((id) => ({ id, role: 'result' })),
  ];
  if (!pending.length) return;
  clearTimeout(roleSyncTimers.get(projectId));
  roleSyncTimers.set(
    projectId,
    setTimeout(async () => {
      roleSyncTimers.delete(projectId);
      for (const { id, role } of pending) {
        await patchProjectItemQuiet(projectId, id, { role });
      }
    }, 800),
  );
}

async function patchProjectItemQuiet(projectId, itemId, patch) {
  // Cập nhật role mà không giật sidebar/focus — khác đường user action.
  try {
    const response = await request(projectPath(projectId, `/items/${encodeURIComponent(itemId)}`), {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    if (!response.ok) return;
    const updated = parseProject(await response.json());
    if (!updated) return;
    projects = [updated, ...projects.filter(({ id }) => id !== updated.id)];
    sortProjects();
    if (updated.id === activeProject) {
      renderResultsGrid();
      renderGenDialog();
    }
  } catch {
    showToast('Chưa đồng bộ được trạng thái tham chiếu.');
  }
}

// ── Hàng thumb tham chiếu trong composer ─────────────────────────────────

// Cap ảnh tham chiếu khả dụng theo mode + vmode hiện tại (0 = không dùng ảnh nào).
function composerRefCap() {
  if (genState.mode === 'image') return MAX_REFS.image;
  const vmode = genState.video.vmode;
  if (vmode === 'frames') return 2;
  if (vmode === 'ingredients') return MAX_REFS.video;
  return 0; // video Text không dùng ảnh
}

function renderComposerRefs() {
  renderComposerFrames();
  if (!composerRefs) return;
  if (isFramesMode()) {
    composerRefs.hidden = true;
    composerRefs.replaceChildren();
    return;
  }
  const items = selectedItems();
  if (!items.length || workspace.hidden) {
    composerRefs.hidden = true;
    composerRefs.replaceChildren();
    return;
  }
  const cap = composerRefCap();
  const imageCount = items.filter((item) => item.kind === 'image').length;
  const chips = items.map((item) => {
    const isImage = item.kind === 'image';
    const imageIndex = isImage
      ? items.slice(0, items.indexOf(item) + 1).filter((entry) => entry.kind === 'image').length
      : 0;
    const overflow = isImage && imageIndex > cap;
    const unusable = !isImage;
    const chip = document.createElement('div');
    chip.className = `composer-ref-chip${overflow ? ' is-overflow' : ''}${unusable ? ' is-unusable' : ''}`;
    chip.title = unusable
      ? 'Video không dùng làm ảnh tham chiếu'
      : overflow
        ? `Vượt giới hạn ${cap} ảnh tham chiếu — sẽ không gửi`
        : item.name || 'Ảnh tham chiếu';
    const img = document.createElement('img');
    img.src = item.url;
    img.alt = '';
    const cross = document.createElement('button');
    cross.type = 'button';
    cross.className = 'composer-ref-remove';
    cross.textContent = '×';
    cross.setAttribute('aria-label', 'Bỏ chọn');
    cross.setAttribute('tabindex', unusable || overflow ? '-1' : '0');
    cross.addEventListener('click', (event) => {
      event.stopPropagation();
      setSelected(item.id, false);
    });
    chip.append(img, cross);
    return chip;
  });
  const counter = document.createElement('span');
  counter.className = 'composer-refs-count';
  const usable = Math.min(imageCount, cap);
  const videoLabels = {
    text: `Text mode — ${imageCount} ảnh không dùng`,
    extend: `Extend — ${imageCount} ảnh không dùng`,
    frames: `Frames ${usable}/2`,
    ingredients: `Ingredients ${usable}/3`,
  };
  counter.textContent = genState.mode === 'video'
    ? videoLabels[genState.video.vmode] ?? videoLabels.text
    : `Ảnh tham chiếu ${usable}/${cap}`;
  composerRefs.replaceChildren(counter, ...chips);
  composerRefs.hidden = false;
}

function setFrameSlot(slot, itemId) {
  if (!activeProject) return;
  const current = getFrameSlots(activeProject);
  const next = { ...current };
  const val = itemId ? String(itemId) : null;
  if (slot === 'start') next.startId = val;
  else next.endId = val;
  vmodeManual = true;
  frameSlotsByProject.set(activeProject, next);
  const uniqueIds = [...new Set([next.startId, next.endId].filter(Boolean))];
  selectionByProject.set(activeProject, uniqueIds);
  selectionChanged({ preserveSlots: true });
}

function swapFrameSlots() {
  if (!activeProject) return;
  const current = getFrameSlots(activeProject);
  if (!current.startId && !current.endId) return;
  const next = { startId: current.endId, endId: current.startId };
  vmodeManual = true;
  frameSlotsByProject.set(activeProject, next);
  const uniqueIds = [...new Set([next.startId, next.endId].filter(Boolean))];
  selectionByProject.set(activeProject, uniqueIds);
  selectionChanged({ preserveSlots: true });
}

function renderFrameSlot(slotName, item) {
  const el = frameSlotEls[slotName];
  if (!el) return;
  const trigger = el.querySelector('.frame-slot-trigger');
  const plus = el.querySelector('.frame-slot-plus');
  const img = el.querySelector('.frame-slot-thumb img');
  const sub = el.querySelector('.frame-slot-sub');
  const clearBtn = el.querySelector('.frame-slot-clear');
  const filled = Boolean(item);
  el.classList.toggle('is-filled', filled);
  el.classList.toggle('is-open', framePickerSlot === slotName && !framePicker.hidden);
  trigger.setAttribute('aria-expanded', String(framePickerSlot === slotName && !framePicker.hidden));
  if (filled) {
    img.src = item.url;
    img.hidden = false;
    plus.hidden = true;
    sub.textContent = itemDisplayName(item, false);
    clearBtn.hidden = false;
  } else {
    img.removeAttribute('src');
    img.hidden = true;
    plus.hidden = false;
    sub.textContent = 'Chọn hoặc tải ảnh';
    clearBtn.hidden = true;
  }
}

function renderFramePickerGrid() {
  if (!framePicker || framePicker.hidden) return;
  const active = activeProjectData();
  const images = (Array.isArray(active?.results) ? active.results : [])
    .filter((item) => item && item.kind === 'image' && typeof item.url === 'string' && item.url)
    .reverse();
  const slots = getFrameSlots(activeProject);
  framePickerTitle.textContent = framePickerSlot === 'end' ? 'Chọn End frame' : 'Chọn Start frame';
  if (!images.length) {
    framePickerGrid.replaceChildren();
    framePickerGrid.hidden = true;
    framePickerEmpty.hidden = false;
    return;
  }
  framePickerEmpty.hidden = true;
  framePickerGrid.hidden = false;
  framePickerGrid.replaceChildren(
    ...images.map((item) => {
      const id = String(item.id);
      const isStart = slots.startId === id;
      const isEnd = slots.endId === id;
      const btn = makeButton('frame-picker-item', '');
      btn.title = itemDisplayName(item, false);
      if ((framePickerSlot === 'start' && isStart) || (framePickerSlot === 'end' && isEnd)) {
        btn.classList.add('is-chosen');
      }
      const img = document.createElement('img');
      img.src = item.url;
      img.alt = itemDisplayName(item, false);
      btn.append(img);
      if (isStart || isEnd) {
        const tag = document.createElement('span');
        tag.className = 'frame-picker-tag';
        tag.textContent = isStart && isEnd ? 'S+E' : isStart ? 'Start' : 'End';
        btn.append(tag);
      }
      btn.addEventListener('click', () => {
        const slot = framePickerSlot || 'start';
        closeFramePicker();
        setFrameSlot(slot, id);
      });
      return btn;
    }),
  );
}

function openFramePicker(slot) {
  if (!composerFrames || composerFrames.hidden) return;
  if (framePickerSlot === slot && !framePicker.hidden) {
    closeFramePicker();
    return;
  }
  setGenDialogOpen(false);
  framePickerSlot = slot;
  framePicker.hidden = false;
  renderComposerFrames();
  renderFramePickerGrid();
}

function closeFramePicker() {
  if (!framePicker || framePicker.hidden) {
    framePickerSlot = null;
    return;
  }
  framePicker.hidden = true;
  framePickerSlot = null;
  renderComposerFrames();
}

function renderComposerFrames() {
  const active = isFramesMode() && !workspace.hidden;
  if (composer) composer.classList.toggle('is-frames', active);
  prompt.placeholder = active
    ? 'Mô tả chuyển động giữa Start frame và End frame…'
    : 'What do you want to create?';
  if (!composerFrames) return;
  if (!active) {
    composerFrames.hidden = true;
    if (framePicker) framePicker.hidden = true;
    framePickerSlot = null;
    return;
  }
  const byId = projectImageById(activeProject);
  const slots = getFrameSlots(activeProject);
  renderFrameSlot('start', slots.startId ? byId.get(slots.startId) ?? null : null);
  renderFrameSlot('end', slots.endId ? byId.get(slots.endId) ?? null : null);
  if (framesSwap) framesSwap.disabled = !(slots.startId || slots.endId);
  composerFrames.hidden = false;
  if (framePicker && !framePicker.hidden) renderFramePickerGrid();
}

for (const slot of ['start', 'end']) {
  const el = frameSlotEls[slot];
  if (!el) continue;
  el.querySelector('.frame-slot-trigger')?.addEventListener('click', (event) => {
    event.stopPropagation();
    openFramePicker(slot);
  });
  el.querySelector('.frame-slot-clear')?.addEventListener('click', (event) => {
    event.stopPropagation();
    setFrameSlot(slot, null);
  });
}

framesSwap?.addEventListener('click', () => swapFrameSlots());
framesExit?.addEventListener('click', () => {
  vmodeManual = false;
  closeFramePicker();
  genState.video.vmode = 'text';
  renderGenDialog();
  renderComposerRefs();
});
framePickerClose?.addEventListener('click', () => closeFramePicker());

const MAX_REF_UPLOAD_BYTES = 12 * 1024 * 1024;
let dragEnterDepth = 0;

function clearDropHighlights() {
  dragEnterDepth = 0;
  if (stageDropOverlay) stageDropOverlay.hidden = true;
  composer?.classList.remove('is-drag-over');
  frameSlotEls.start?.classList.remove('is-drag-over');
  frameSlotEls.end?.classList.remove('is-drag-over');
  framePicker?.classList.remove('is-drag-over');
}

function updateDropHighlights(target) {
  if (stageDropOverlay) stageDropOverlay.hidden = false;
  const el = target instanceof Element ? target : null;
  const onStart = Boolean(el?.closest('#frame-slot-start'));
  const onEnd = Boolean(el?.closest('#frame-slot-end'));
  const onPicker = Boolean(el?.closest('#frame-picker'));
  const onComposer = Boolean(el?.closest('.composer')) && !onStart && !onEnd && !onPicker;
  composer?.classList.toggle('is-drag-over', onComposer);
  frameSlotEls.start?.classList.toggle('is-drag-over', onStart);
  frameSlotEls.end?.classList.toggle('is-drag-over', onEnd);
  framePicker?.classList.toggle('is-drag-over', onPicker);
}

function isFileDragEvent(event) {
  const types = event?.dataTransfer?.types;
  return Boolean(types && Array.from(types).includes('Files'));
}

function isModalOpen() {
  return [
    document.querySelector('#lightbox'),
    document.querySelector('#video-editor'),
    document.querySelector('#mask-editor'),
    document.querySelector('#wm-modal'),
    document.querySelector('#item-dialog'),
  ].some((el) => el && !el.hidden);
}

function guessImageMime(file) {
  if (!file) return null;
  const type = String(file.type || '').toLowerCase();
  if (/^image\/(png|jpeg|jpg|webp)$/i.test(type)) return type === 'image/jpg' ? 'image/jpeg' : type;
  if (type.startsWith('image/')) return type;
  const name = String(file.name || '').toLowerCase();
  if (name.endsWith('.png')) return 'image/png';
  if (name.endsWith('.jpg') || name.endsWith('.jpeg')) return 'image/jpeg';
  if (name.endsWith('.webp')) return 'image/webp';
  if (/\.(gif|bmp|avif|svg|ico|tif|tiff)$/i.test(name)) return 'image/png';
  return null;
}

function normalizeDataUrlPrefix(dataUrl, mime) {
  if (!dataUrl || !mime) return dataUrl;
  return dataUrl.replace(/^data:(?:application\/octet-stream|);base64,/i, `data:${mime};base64,`);
}

async function encodeImageViaCanvas(file) {
  const objectUrl = URL.createObjectURL(file);
  try {
    const img = await new Promise((resolve, reject) => {
      const el = new Image();
      el.onload = () => resolve(el);
      el.onerror = () => reject(new Error('decode failed'));
      el.src = objectUrl;
    });
    const width = img.naturalWidth || img.width;
    const height = img.naturalHeight || img.height;
    if (!width || !height) return '';
    const canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return '';
    ctx.drawImage(img, 0, 0, width, height);
    let dataUrl = canvas.toDataURL('image/png');
    if (dataUrl.length <= 15_500_000) return dataUrl;
    dataUrl = canvas.toDataURL('image/jpeg', 0.92);
    if (dataUrl.length <= 15_500_000) return dataUrl;
    return canvas.toDataURL('image/jpeg', 0.8);
  } catch {
    return '';
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

async function readImageFileAsDataUrl(file) {
  const mime = guessImageMime(file);
  if (!mime) return { ok: false, reason: 'type' };
  const isNative = /^image\/(png|jpeg|webp)$/i.test(mime);
  if (isNative && file.size > 0 && file.size <= MAX_REF_UPLOAD_BYTES) {
    const raw = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error('read failed'));
      reader.readAsDataURL(file);
    }).catch(() => '');
    const fixed = normalizeDataUrlPrefix(raw, mime);
    if (/^data:image\/(png|jpeg|webp);base64,/i.test(fixed)) {
      return { ok: true, dataUrl: fixed };
    }
  }
  const converted = await encodeImageViaCanvas(file);
  if (!converted) {
    return { ok: false, reason: file.size > MAX_REF_UPLOAD_BYTES ? 'size' : 'decode' };
  }
  return { ok: true, dataUrl: converted };
}

async function uploadReferenceFiles(rawFiles, { target = 'refs', slot = null } = {}) {
  if (workspace.hidden) return;
  const files = [...(rawFiles || [])].filter(Boolean);
  if (!files.length) return;

  const prepared = [];
  let badTypeCount = 0;
  let badReadCount = 0;
  for (const file of files) {
    const res = await readImageFileAsDataUrl(file);
    if (res.ok) {
      const cleanName = String(file.name || '').replace(/\.[^.]+$/, '').trim().slice(0, 120) || 'Tham chiếu';
      prepared.push({ name: cleanName, image: res.dataUrl });
    } else if (res.reason === 'type') {
      badTypeCount += 1;
    } else {
      badReadCount += 1;
    }
  }

  if (!prepared.length) {
    if (badTypeCount) {
      showToast('Chỉ hỗ trợ tệp ảnh (PNG, JPEG, WebP…) để tải lên Playground làm tham chiếu.');
    } else if (badReadCount) {
      showToast('Ảnh quá lớn (tối đa 12MB) hoặc không đọc được.');
    }
    return;
  }

  const project = await ensureActiveProject();
  if (!project) return;

  const uploadedIds = [];
  for (const entry of prepared) {
    const latest = projects.find(({ id }) => id === project.id) ?? project;
    const beforeIds = new Set((latest.results ?? []).map((item) => String(item?.id)));
    const updated = await createReferenceItem(project.id, entry, { silent: true });
    if (!updated) break;
    const added = (updated.results ?? []).find((item) => !beforeIds.has(String(item?.id)))
      ?? (updated.results ?? [])[(updated.results ?? []).length - 1];
    if (added?.id) uploadedIds.push(String(added.id));
  }

  if (!uploadedIds.length) return;

  if (target === 'frame-slot') {
    const targetSlot = slot || 'start';
    closeFramePicker();
    setFrameSlot(targetSlot, uploadedIds[0]);
    showToast(
      `Đã tải ảnh lên Playground và đặt làm ${targetSlot === 'end' ? 'End' : 'Start'} frame.`,
    );
    return;
  }

  if (target === 'composer-frames' && isFramesMode()) {
    closeFramePicker();
    const slots = getFrameSlots(project.id);
    if (uploadedIds.length === 1) {
      const nextSlot = !slots.startId ? 'start' : (!slots.endId ? 'end' : 'start');
      setFrameSlot(nextSlot, uploadedIds[0]);
    } else {
      const next = { startId: uploadedIds[0], endId: uploadedIds[1] };
      vmodeManual = true;
      frameSlotsByProject.set(project.id, next);
      selectionByProject.set(project.id, [next.startId, next.endId]);
      selectionChanged({ preserveSlots: true });
    }
    showToast(`Đã tải ${uploadedIds.length} ảnh lên Playground cho Frames (không tải lên Flow).`);
    return;
  }

  // Giữ selection cũ + chọn thêm ảnh vừa thả để dùng làm tham chiếu ngay (không gửi lên Flow lúc tải).
  const current = selectionIds(project.id);
  const nextSelection = [...current];
  for (const id of uploadedIds) {
    if (!nextSelection.includes(id)) nextSelection.push(id);
  }
  selectionByProject.set(project.id, nextSelection);
  selectionChanged();

  const warnSuffix = badTypeCount || badReadCount ? ' (một số tệp không phải ảnh đã được bỏ qua)' : '';
  showToast(
    uploadedIds.length === 1
      ? `Đã tải ảnh lên Playground làm tham chiếu (chưa tải lên Flow)${warnSuffix}.`
      : `Đã tải ${uploadedIds.length} ảnh lên Playground làm tham chiếu (chưa tải lên Flow)${warnSuffix}.`,
  );
}

frameUploadInput?.addEventListener('change', async (event) => {
  const files = [...(event.target.files || [])];
  event.target.value = '';
  await uploadReferenceFiles(files, { target: 'frame-slot', slot: framePickerSlot || 'start' });
});

refUploadInput?.addEventListener('change', async (event) => {
  const files = [...(event.target.files || [])];
  event.target.value = '';
  await uploadReferenceFiles(files, { target: isFramesMode() ? 'composer-frames' : 'refs' });
});

document.addEventListener('dragenter', (event) => {
  if (workspace.hidden || isModalOpen() || !isFileDragEvent(event)) return;
  event.preventDefault();
  dragEnterDepth += 1;
  updateDropHighlights(event.target);
});

document.addEventListener('dragover', (event) => {
  if (workspace.hidden || isModalOpen() || !isFileDragEvent(event)) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
  updateDropHighlights(event.target);
});

document.addEventListener('dragleave', (event) => {
  if (!isFileDragEvent(event)) return;
  dragEnterDepth = Math.max(0, dragEnterDepth - 1);
  if (dragEnterDepth === 0 || !event.relatedTarget) clearDropHighlights();
});

document.addEventListener('drop', async (event) => {
  const hadFiles = isFileDragEvent(event) || Boolean(event.dataTransfer?.files?.length);
  clearDropHighlights();
  if (workspace.hidden || !hadFiles) return;
  event.preventDefault();
  event.stopPropagation();
  if (isModalOpen()) return;
  const files = event.dataTransfer?.files;
  if (!files || !files.length) return;
  const el = event.target instanceof Element ? event.target : null;
  if (el?.closest('#frame-slot-start')) {
    await uploadReferenceFiles(files, { target: 'frame-slot', slot: 'start' });
  } else if (el?.closest('#frame-slot-end')) {
    await uploadReferenceFiles(files, { target: 'frame-slot', slot: 'end' });
  } else if (el?.closest('#frame-picker')) {
    await uploadReferenceFiles(files, { target: 'frame-slot', slot: framePickerSlot || 'start' });
  } else if (el?.closest('.composer') && isFramesMode()) {
    await uploadReferenceFiles(files, { target: 'composer-frames' });
  } else {
    await uploadReferenceFiles(files, { target: 'refs' });
  }
});

document.addEventListener('dragend', clearDropHighlights);
window.addEventListener('blur', clearDropHighlights);

document.addEventListener('paste', async (event) => {
  if (workspace.hidden || isModalOpen()) return;
  const files = [...(event.clipboardData?.files || [])].filter((f) => guessImageMime(f));
  if (!files.length) return;
  const plainText = event.clipboardData?.getData?.('text/plain') || '';
  if (!plainText) event.preventDefault();
  await uploadReferenceFiles(files, { target: isFramesMode() ? 'composer-frames' : 'refs' });
});

async function loadProjects() {
  const response = await request('/playground/api/projects');
  if (!response.ok) throw new Error('Could not load projects');
  const rows = await response.json();
  projects = Array.isArray(rows) ? rows.map(parseProject).filter(Boolean) : [];
  sortProjects();
  activeProject = projects[0]?.id ?? null;
  renderProjects();
  showProjectContents();
}

export async function renderResults(items) {
  if (workspace.hidden) return;
  const active = projects.find(({ id }) => id === activeProject);
  if (!active) return;
  try {
    const response = await request(projectPath(active.id), {
      method: 'PATCH',
      body: JSON.stringify({ results: items }),
    });
    if (!response.ok) throw new Error('Could not save results');
    const updated = parseProject(await response.json());
    if (updated) {
      projects = [updated, ...projects.filter(({ id }) => id !== updated.id)];
      sortProjects();
      renderProjects(active.id);
    }
  } catch {
    showToast('Không thể lưu kết quả cho project.');
  }
}

// --- Results grid -----------------------------------------------------------

function aspectFromRatio(ratio) {
  if (typeof ratio !== 'string' || !/^\d+:\d+$/.test(ratio)) return null;
  const [width, height] = ratio.split(':').map(Number);
  if (!width || !height) return null;
  return `${width} / ${height}`;
}

function expandIcon() {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '14');
  svg.setAttribute('height', '14');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7');
  svg.append(path);
  return svg;
}

function createResultCard(item) {
  if (!item || typeof item.url !== 'string') return null;
  const isVideo = item.kind === 'video';
  const card = document.createElement('figure');
  card.className = 'result-card';
  if (item.id !== undefined && item.id !== null) {
    card.dataset.resultId = String(item.id);
    if (isSelected(item.id)) card.classList.add('is-selected');
  }
  const media = document.createElement(isVideo ? 'video' : 'img');
  if (isVideo) {
    media.controls = true;
    media.preload = 'metadata';
  } else {
    media.loading = 'lazy';
    media.alt = '';
    card.classList.add('is-image');
  }
  media.src = item.url;
  media.style.aspectRatio = aspectFromRatio(item?.meta?.ratio) ?? (isVideo ? '16 / 9' : '1 / 1');
  if (!isVideo && !item?.meta?.ratio && item?.meta?.source === 'playground-reference') {
    media.addEventListener('load', () => {
      if (media.naturalWidth > 0 && media.naturalHeight > 0) {
        const clamped = Math.min(Math.max(media.naturalWidth / media.naturalHeight, 0.45), 2.2);
        media.style.aspectRatio = String(clamped);
      }
    });
  }
  card.append(media);
  if (item.role === 'reference') {
    const badge = document.createElement('span');
    badge.className = 'result-ref-badge';
    badge.textContent = 'REF';
    card.append(badge);
  }
  if (item.id !== undefined && item.id !== null) {
    const menuButton = makeButton('result-menu', '');
    menuButton.setAttribute('aria-label', `Tuỳ chọn item${item.name ? `: ${item.name}` : ''}`);
    menuButton.title = 'Tuỳ chọn item';
    menuButton.setAttribute('aria-expanded', 'false');
    const dot = document.createElement('span');
    dot.className = 'result-menu-dot';
    dot.textContent = '⋯';
    menuButton.append(dot);
    menuButton.addEventListener('click', (event) => {
      event.stopPropagation();
      const alreadyOpen = openResultMenu?.toggle === menuButton;
      closeResultMenu();
      if (alreadyOpen) return;
      openResultMenuFor(menuButton, item);
    });
    card.append(menuButton);
  }
  if (isVideo) {
    // Nút góc mở video editor; click vùng hình của video cũng mở editor (trừ
    // khi click trúng controls — xem heuristic media events bên dưới).
    const expand = makeButton('result-expand', '');
    expand.setAttribute('aria-label', 'Mở trình chỉnh sửa video');
    expand.title = 'Mở trình chỉnh sửa video';
    expand.append(expandIcon());
    card.append(expand);
    // Controls của <video> là shadow DOM — không đánh dấu được click riêng, nên
    // nghe các media event mà controls phát ra trong 600ms sau pointerdown;
    // có event (play/pause/seek/âm lượng…) → click vừa rồi là tương tác
    // controls, không mở editor.
    let controlsUsed = false;
    let controlsArmTimer = null;
    const CONTROL_EVENTS = ['play', 'pause', 'seeking', 'seeked', 'volumechange', 'ratechange'];
    const markControls = () => { controlsUsed = true; };
    const clearControlArming = () => {
      clearTimeout(controlsArmTimer);
      for (const type of CONTROL_EVENTS) media.removeEventListener(type, markControls);
    };
    media.addEventListener('pointerdown', () => {
      controlsUsed = false;
      for (const type of CONTROL_EVENTS) media.addEventListener(type, markControls);
      clearTimeout(controlsArmTimer);
      controlsArmTimer = setTimeout(clearControlArming, 600);
    });
    media.addEventListener('click', (event) => {
      const used = controlsUsed;
      clearControlArming();
      controlsUsed = false;
      if (used) return; // click trên play/seek/âm lượng… — để controls tự xử
      event.stopPropagation();
      if (resultItemInProject(item)) openVideoEditor(item);
    });
  }
  // Hover overlay: chỉ còn tên item (hành động đã dồn vào menu ⋯). Tên dài
  // được cắt bằng CSS ellipsis nên cả tên user tự đặt cũng không phá layout.
  const overlay = document.createElement('figcaption');
  overlay.className = 'result-overlay';
  const label = document.createElement('span');
  label.className = 'result-name';
  label.textContent = itemDisplayName(item, isVideo);
  overlay.append(label);
  card.append(overlay);
  return card;
}

function itemDisplayName(item, isVideo) {
  const name = typeof item.name === 'string' ? item.name.trim() : '';
  return name || (isVideo ? 'Video' : 'Ảnh');
}

// "Re use prompt": điền prompt của item vào composer + chọn lại các ảnh tham
// chiếu lúc generate (chỉ id còn tồn tại trong project, đúng thứ tự trong meta).
function reuseItemPrompt(item) {
  // Re-use prompt đè vmode/model → đang nối tiếp thì hủy extend trước.
  if (genState.video.vmode === 'extend') exitExtendMode();
  const promptText = typeof item.meta?.prompt === 'string' ? item.meta.prompt.trim() : '';
  if (!promptText) return;
  const mode = item.kind === 'video' ? 'video' : 'image';
  genState.mode = mode;
  if (mode === 'video') {
    // Chuyển đúng vmode lúc generate; 'extend' không tái dùng được → về 'text'.
    const vmode = typeof item.meta?.vmode === 'string' ? item.meta.vmode : 'text';
    genState.video.vmode = ['text', 'frames', 'ingredients'].includes(vmode) ? vmode : 'text';
  }
  // Theo luôn model dùng lúc generate nếu catalog vẫn còn model đó.
  const modelId = typeof item.meta?.model === 'string' ? item.meta.model : '';
  const matchedModel = modelId
    ? optionsFor(mode).models.find(
        (model) => model.id === modelId || (modelId === 'nano-banana-pro' && model.base === 'gemini-3.0-pro-image'),
      )
    : null;
  if (matchedModel) genState[mode].model = matchedModel.id;
  if (mode === 'video' && genState.video.vmode === 'frames') vmodeManual = true;
  applyModelCapability(mode);
  prompt.value = promptText;
  // Bắn input để autosave draft + auto-grow textarea chạy đúng đường hiện có.
  prompt.dispatchEvent(new Event('input'));
  const liveImageIds = new Set(
    (activeProjectData()?.results ?? [])
      .filter((candidate) => candidate?.kind === 'image')
      .map((candidate) => String(candidate.id)),
  );
  clearSelection();
  const references = Array.isArray(item.meta?.references) ? item.meta.references : [];
  for (const reference of references) {
    const id = String(reference);
    if (liveImageIds.has(id)) setSelected(id, true);
  }
  if (mode === 'video' && genState.video.vmode === 'frames' && activeProject) {
    const validRefs = references.map((r) => String(r)).filter((id) => liveImageIds.has(id));
    frameSlotsByProject.set(activeProject, { startId: validRefs[0] ?? null, endId: validRefs[1] ?? null });
    renderComposerRefs();
  }
  renderGenDialog();
  showToast('Đã điền lại prompt từ item này.');
}

function createCardAspectFrame(ratio, mode) {
  const frame = document.createElement('div');
  frame.className = 'result-card-frame';
  frame.setAttribute('aria-hidden', 'true');
  frame.style.aspectRatio = aspectFromRatio(ratio) ?? (mode === 'video' ? '16 / 9' : '1 / 1');
  return frame;
}

function createPendingCard(pending) {
  const card = document.createElement('div');
  card.className = 'result-card is-pending';
  card.setAttribute('role', 'status');
  card.setAttribute('aria-busy', 'true');
  card.setAttribute('aria-label', pending.mode === 'video' ? 'Đang tạo video…' : 'Đang tạo ảnh…');
  const spinner = document.createElement('span');
  spinner.className = 'result-spinner';
  card.append(createCardAspectFrame(pending.ratio, pending.mode), spinner);
  return card;
}

function makeFailedActionButton(className, iconChar, labelText) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = className;
  const icon = document.createElement('span');
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = iconChar;
  const text = document.createElement('span');
  text.className = 'result-failed-btn-text';
  text.textContent = ` ${labelText}`;
  button.append(icon, text);
  return button;
}

function createFailedCard(job) {
  // Thẻ thay mặt job THẤT BẠI: giữ đúng khung chiều cao card thường, nội dung
  // nằm trong overlay inset:0 để không làm phình chiều cao hàng/card.
  const card = document.createElement('div');
  card.className = 'result-card is-failed';
  card.setAttribute('role', 'status');
  card.setAttribute('aria-label', 'Tạo nội dung thất bại');
  const errorText = job.error || 'Không tạo được nội dung.';
  card.title = errorText;
  card.addEventListener('click', () => showToast(errorText));

  const body = document.createElement('div');
  body.className = 'result-failed-body';
  const label = document.createElement('span');
  label.className = 'result-failed-label';
  label.textContent = job.mode === 'video' ? 'Tạo video thất bại' : 'Tạo ảnh thất bại';
  const reason = document.createElement('span');
  reason.className = 'result-failed-reason';
  reason.textContent = errorText;
  const actions = document.createElement('div');
  actions.className = 'result-failed-actions';
  const retry = makeFailedActionButton('result-failed-retry', '↻', 'Thử lại');
  retry.setAttribute('aria-label', 'Thử lại');
  retry.title = 'Chạy lại với cùng tham số';
  retry.addEventListener('click', (event) => {
    event.stopPropagation();
    retryFailedJob(job.jobId);
  });
  const remove = makeFailedActionButton('result-failed-delete', '✕', 'Xóa');
  remove.setAttribute('aria-label', 'Xóa');
  remove.title = 'Xóa thông báo lỗi này';
  remove.addEventListener('click', (event) => {
    event.stopPropagation();
    deleteFailedJob(job.jobId);
  });
  actions.append(retry, remove);
  body.append(label, reason, actions);
  card.append(createCardAspectFrame(job.ratio, job.mode), body);
  return card;
}

function renderResultsGrid() {
  const active = projects.find(({ id }) => id === activeProject);
  if (workspace.hidden || !active) {
    resultsGrid.replaceChildren();
    resultsGrid.hidden = true;
    resultsEmpty.hidden = true;
    return;
  }
  const results = Array.isArray(active.results) ? active.results : [];
  const cards = [];
  // Mỗi job đang chạy (gồm cả POST chưa có job_id) là một nhóm pending riêng;
  // sắp theo startedAt giảm dần để job mới nhất bên trái. Kết quả partial của
  // job chiếm slot từ đầu nhóm, spinner lấp phần còn lại cho đủ count.
  const projectJobs = [...jobsById.values()]
    .filter((job) => job.projectId === active.id)
    .sort((a, b) => b.startedAt - a.startedAt);
  for (const job of projectJobs) {
    if (job.status === 'error') {
      cards.push(createFailedCard(job));
      continue;
    }
    const partial = (Array.isArray(job.partial) ? job.partial : []).slice(0, job.count).reverse();
    for (const item of partial) {
      const card = createResultCard(item);
      if (card) cards.push(card);
    }
    let filled = Math.min(partial.length, job.count);
    while (filled < job.count) {
      cards.push(createPendingCard(job));
      filled += 1;
    }
  }
  // Mới nhất luôn ở bên trái: đảo thứ tự lúc render (mới → cũ); thứ tự lưu trong
  // DB giữ nguyên vì item mới luôn được append vào cuối mảng results.
  for (const item of [...results].reverse()) {
    const card = createResultCard(item);
    if (card) cards.push(card);
  }
  resultsGrid.replaceChildren(...cards);
  resultsGrid.hidden = cards.length === 0;
  resultsEmpty.hidden = cards.length > 0;
}

// --- Gallery lightbox (ảnh) --------------------------------------------------
// Click item ảnh mở gallery duyệt TẤT CẢ ảnh của project active theo thứ tự
// grid (mới nhất trước). Video KHÔNG vào gallery — click video mở video editor
// (xem phần Video editor bên dưới).

const lightbox = document.createElement('div');
lightbox.id = 'lightbox';
lightbox.className = 'lightbox';
lightbox.hidden = true;
lightbox.setAttribute('role', 'dialog');
lightbox.setAttribute('aria-modal', 'true');
lightbox.setAttribute('aria-label', 'Xem ảnh');
document.body.append(lightbox);

// State gallery: ảnh theo đúng thứ tự grid + index ảnh đang mở.
let galleryState = null; // { items: [item], index }

function galleryImages() {
  const project = activeProjectData();
  return (Array.isArray(project?.results) ? project.results : [])
    .filter((item) => item?.kind === 'image' && typeof item.url === 'string' && item.url)
    .reverse();
}

// DOM gallery dựng một lần, nội dung cập nhật theo galleryState.
const lightboxImg = document.createElement('img');
lightboxImg.className = 'lightbox-img';
lightboxImg.alt = '';
const lightboxTitle = document.createElement('span');
lightboxTitle.className = 'lightbox-title';
const lightboxMeta = document.createElement('span');
lightboxMeta.className = 'lightbox-meta';
const lightboxDownload = makeButton('lightbox-btn', 'Download');
lightboxDownload.setAttribute('aria-label', 'Tải ảnh này xuống');
lightboxDownload.addEventListener('click', () => {
  const item = galleryState?.items[galleryState.index];
  if (item) downloadItem(item);
});
const lightboxClose = makeButton('lightbox-close', '×');
lightboxClose.setAttribute('aria-label', 'Đóng');
lightboxClose.addEventListener('click', closeLightbox);
const lightboxHead = document.createElement('div');
lightboxHead.className = 'lightbox-head';
lightboxHead.append(lightboxTitle, lightboxMeta, lightboxDownload, lightboxClose);
const lightboxPrev = makeButton('lightbox-nav lightbox-prev', '‹');
lightboxPrev.setAttribute('aria-label', 'Ảnh mới hơn (trước)');
lightboxPrev.addEventListener('click', () => galleryShift(-1));
const lightboxNext = makeButton('lightbox-nav lightbox-next', '›');
lightboxNext.setAttribute('aria-label', 'Ảnh cũ hơn (tiếp)');
lightboxNext.addEventListener('click', () => galleryShift(1));
lightbox.append(lightboxHead, lightboxImg, lightboxPrev, lightboxNext);

function renderGallery() {
  const state = galleryState;
  if (!state || !state.items.length) {
    closeLightbox();
    return;
  }
  state.index = Math.min(Math.max(state.index, 0), state.items.length - 1);
  const item = state.items[state.index];
  const res = typeof item.meta?.res === 'string' && item.meta.res ? item.meta.res : '';
  // Kích thước pixel thật chỉ có sau khi ảnh decode xong; trước đó hiện "…".
  const syncMeta = () => {
    if (lightboxImg.naturalWidth > 0 && lightboxImg.naturalHeight > 0) {
      lightboxMeta.textContent = [res, `${lightboxImg.naturalWidth}×${lightboxImg.naturalHeight}`]
        .filter(Boolean)
        .join(' · ');
    }
  };
  lightboxImg.onload = syncMeta;
  lightboxImg.src = item.url;
  if (lightboxImg.complete) syncMeta(); // ảnh đã cache: onload có thể không fire lại
  lightboxTitle.textContent = itemDisplayName(item, false);
  lightboxTitle.title = lightboxTitle.textContent;
  lightboxPrev.disabled = state.index === 0;
  lightboxNext.disabled = state.index === state.items.length - 1;
}

function galleryShift(delta) {
  if (!galleryState) return;
  const next = galleryState.index + delta;
  if (next < 0 || next >= galleryState.items.length) return;
  galleryState.index = next;
  renderGallery();
}

function openGalleryForItem(item) {
  if (!item) return;
  const items = galleryImages();
  if (!items.length) return;
  const index = items.findIndex((candidate) => String(candidate?.id) === String(item.id));
  if (index < 0) return; // card không nằm trong danh sách ảnh (url rỗng…) → không mở
  galleryState = { items, index };
  renderGallery();
  lightbox.hidden = false;
  lightboxClose.focus();
}

function closeLightbox() {
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  galleryState = null;
  // Nhả src + bỏ onload để hủy decode ảnh đang treo.
  lightboxImg.removeAttribute('src');
  lightboxImg.onload = null;
}

lightbox.addEventListener('click', (event) => {
  // composedPath: same re-render-safe pattern as the dialog click-outside.
  if (event.composedPath()[0] === lightbox) closeLightbox();
});

// Item hoàn thật sự nằm trong project.results (loại card pending/partial của
// job đang chạy — những card đó KHÔNG mở được gallery/editor).
function resultItemInProject(item) {
  const project = activeProjectData();
  if (!project || !item) return null;
  return (project.results ?? []).find((candidate) => String(candidate?.id) === String(item.id)) ?? null;
}

resultsGrid.addEventListener('click', (event) => {
  const path = event.composedPath();
  const card = path.find(
    (node) => node instanceof Element && node.classList.contains('result-card') && !node.classList.contains('is-pending')
  );
  if (!card) return;
  // Ctrl/Cmd + click = toggle chọn item làm tham chiếu (không mở gallery/editor).
  if (event.ctrlKey || event.metaKey) {
    const resultId = card.dataset.resultId;
    if (resultId) {
      event.preventDefault();
      setSelected(resultId, !isSelected(resultId));
    }
    return;
  }
  const media = card.querySelector('img, video');
  if (!media) return;
  const resultId = card.dataset.resultId;
  const item = resultId ? resultItemInProject({ id: resultId }) : null;
  if (!item) return; // pending/partial: chỉ item hoàn tất mới mở được
  const onExpand = path.some((node) => node instanceof Element && node.classList.contains('result-expand'));
  // Inline video owns its own clicks: click vùng hình tự mở editor từ listener
  // trong createResultCard; ở đây chỉ nút expand mở editor. Ảnh mở gallery.
  if (media.tagName === 'VIDEO') {
    if (onExpand) openVideoEditor(item);
    return;
  }
  openGalleryForItem(item);
});

// ── Video editor modal (trim + extend) ──────────────────────────────────────
// Mở khi click item video hoàn tất (vùng hình hoặc nút expand góc). Trim: kéo
// 2 handle đặt [start, end) giữ lại → POST /items/{id}/trim → 201 item mới →
// merge vào project.results local (pattern mergeJobResults) + render. Extend:
// đóng editor và bật chế độ nối tiếp trên composer (vmode 'extend').

const videoEditor = document.querySelector('#video-editor');
const videoEditorTitle = document.querySelector('#video-editor-title');
const videoEditorPlayer = document.querySelector('#video-editor-player');
const videoEditorClose = document.querySelector('#video-editor-close');
const trimTrack = document.querySelector('#trim-track');
const trimRange = document.querySelector('#trim-range');
const trimHandleStart = document.querySelector('#trim-handle-start');
const trimHandleEnd = document.querySelector('#trim-handle-end');
const trimStartLabel = document.querySelector('#trim-start-label');
const trimDurationLabel = document.querySelector('#trim-duration-label');
const trimEndLabel = document.querySelector('#trim-end-label');
const videoEditorExtend = document.querySelector('#video-editor-extend');
const videoEditorTrimSave = document.querySelector('#video-editor-trim-save');

// Chip "Nối tiếp" nổi trên composer khi đang ở chế độ extend.
const extendChip = document.querySelector('#extend-chip');
const extendChipLabel = document.querySelector('#extend-chip-label');
const extendChipCancel = document.querySelector('#extend-chip-cancel');

// Mirror TRIM_MIN_DURATION_SEC backend: đoạn giữ lại tối thiểu 0.2s.
const TRIM_MIN_SEC = 0.2;

let trimState = null; // { item, projectId, duration, start, end, saving, dragging }
let extendState = null; // { itemId, model, ratio, name } — video đang nối tiếp

// Label hiển thị của 1 model id trong catalog; không thấy → null (caller tự
// fallback sang id gốc dạng text).
function modelLabelForId(mode, modelId) {
  return optionsFor(mode).models.find((model) => model.id === modelId)?.label ?? null;
}

function fmtClock(sec) {
  if (!Number.isFinite(sec)) return '–:––';
  const total = Math.floor(Math.max(0, sec));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

// Giá trị trim thực sự gửi server (3 số thập phân — khớp độ chính xác -ss/-t
// của ffmpeg). Validate trên giá trị ĐÃ round để không xảy ra client-pass mà
// server 400 do sai số float (vd 0.199999…).
function roundedTrimRange() {
  if (!trimState) return null;
  return {
    start: Math.round(trimState.start * 1000) / 1000,
    end: Math.round(trimState.end * 1000) / 1000,
  };
}

function trimRangeValid() {
  if (!trimState) return false;
  const { duration } = trimState;
  if (!Number.isFinite(duration) || duration <= 0) return false;
  const { start, end } = roundedTrimRange();
  if (!(start >= 0)) return false;
  // Dung sai +0.0005s cho end: round 3 số có thể đẩy end (== duration lẻ như
  // 7.999999…) vượt lên 0.0005 — vẫn dưới tolerance +0.5s của server.
  if (!(end <= duration + 0.0005)) return false;
  return end - start >= TRIM_MIN_SEC;
}

function renderTrimTimeline() {
  const state = trimState;
  if (!state) return;
  const dur = state.duration;
  const metadataOk = Number.isFinite(dur) && dur > 0;
  const pct = (sec) => (metadataOk ? `${Math.min(100, Math.max(0, (sec / dur) * 100))}%` : '0%');
  trimRange.style.left = pct(state.start);
  trimRange.style.width = metadataOk
    ? `${Math.min(100, Math.max(0, ((state.end - state.start) / dur) * 100))}%`
    : '0%';
  trimHandleStart.style.left = pct(state.start);
  trimHandleEnd.style.left = pct(state.end);
  trimHandleStart.disabled = !metadataOk;
  trimHandleEnd.disabled = !metadataOk;
  trimStartLabel.textContent = fmtClock(state.start);
  trimEndLabel.textContent = fmtClock(state.end);
  trimDurationLabel.textContent = fmtClock(dur);
  videoEditorTrimSave.disabled = state.saving || !trimRangeValid();
}

// Điều kiện Extend của 1 item: phải biết model + ratio lúc generate, và ratio
// thuộc 2 tỉ lệ upstream hỗ trợ cho extend (16:9 landscape / 9:16 portrait).
function extendCapabilityFor(item) {
  const ratio = typeof item?.meta?.ratio === 'string' ? item.meta.ratio : '';
  const model = typeof item?.meta?.model === 'string' ? item.meta.model : '';
  if (!ratio || !model) {
    return { ok: false, reason: 'Video này thiếu thông tin model/tỉ lệ — không thể extend.' };
  }
  if (ratio !== '16:9' && ratio !== '9:16') {
    return { ok: false, reason: 'Extend chỉ hỗ trợ tỉ lệ 16:9/9:16.' };
  }
  return { ok: true, reason: '' };
}

function openVideoEditor(item) {
  if (!item || item.kind !== 'video' || typeof item.url !== 'string' || !activeProject) return;
  trimState = {
    item,
    projectId: activeProject,
    duration: Number.NaN,
    start: 0,
    end: 0,
    saving: false,
    dragging: null,
  };
  videoEditorTitle.textContent = itemDisplayName(item, true);
  videoEditorTitle.title = videoEditorTitle.textContent;
  const capability = extendCapabilityFor(item);
  videoEditorExtend.disabled = !capability.ok;
  videoEditorExtend.title = capability.ok ? 'Tạo video nối tiếp từ video này' : capability.reason;
  renderTrimTimeline(); // duration chưa có → handle disable + nút lưu disable
  videoEditor.hidden = false;
  videoEditorClose.focus();
}

function closeVideoEditor() {
  if (videoEditor.hidden) return;
  videoEditor.hidden = true;
  trimState = null;
  // Nhả src để dừng playback + giải phóng buffer; mở lại sẽ gắn src mới.
  videoEditorPlayer.removeAttribute('src');
  videoEditorPlayer.load();
}

videoEditorClose.addEventListener('click', closeVideoEditor);
videoEditor.addEventListener('click', (event) => {
  // Click nền (không trúng card) đóng editor — same pattern với item-dialog.
  if (event.composedPath()[0] === videoEditor) closeVideoEditor();
});

videoEditorPlayer.addEventListener('loadedmetadata', () => {
  if (!trimState) return;
  const duration = videoEditorPlayer.duration;
  if (Number.isFinite(duration) && duration > 0) {
    trimState.duration = duration;
    trimState.start = 0;
    trimState.end = duration;
  }
  renderTrimTimeline(); // duration NaN (metadata lỗi) → mọi thứ vẫn disable
});

// Timeline: 2 handle kéo bằng pointer events; kéo bị chặn biên [0, duration]
// và khoảng cách tối thiểu TRIM_MIN_SEC giữa 2 đầu (range sai → nút lưu disable).
function trimPointerSec(event) {
  const rect = trimTrack.getBoundingClientRect();
  const ratio = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0;
  return Math.min(1, Math.max(0, ratio)) * (trimState?.duration ?? Number.NaN);
}

function trimHandlePointerDown(kind) {
  return (event) => {
    if (!trimState || trimState.dragging || !Number.isFinite(trimState.duration)) return;
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    event.preventDefault();
    trimState.dragging = kind;
    event.currentTarget.setPointerCapture(event.pointerId);
  };
}

function trimHandlePointerMove(kind) {
  return (event) => {
    if (!trimState || trimState.dragging !== kind) return;
    const sec = trimPointerSec(event);
    if (!Number.isFinite(sec)) return;
    if (kind === 'start') {
      trimState.start = Math.min(Math.max(sec, 0), trimState.end - TRIM_MIN_SEC);
    } else {
      trimState.end = Math.min(Math.max(sec, trimState.start + TRIM_MIN_SEC), trimState.duration);
    }
    renderTrimTimeline();
  };
}

function trimHandlePointerUp() {
  if (trimState) trimState.dragging = null;
}

trimHandleStart.addEventListener('pointerdown', trimHandlePointerDown('start'));
trimHandleEnd.addEventListener('pointerdown', trimHandlePointerDown('end'));
trimHandleStart.addEventListener('pointermove', trimHandlePointerMove('start'));
trimHandleEnd.addEventListener('pointermove', trimHandlePointerMove('end'));
for (const type of ['pointerup', 'pointercancel']) {
  trimHandleStart.addEventListener(type, trimHandlePointerUp);
  trimHandleEnd.addEventListener(type, trimHandlePointerUp);
}

// Bàn phím cho handle: ←/→ nudge ±0.1s (Shift ±1s) — điều chỉnh mịn sau khi kéo.
function nudgeTrim(kind, delta) {
  if (!trimState || !Number.isFinite(trimState.duration) || trimState.saving) return;
  if (kind === 'start') {
    trimState.start = Math.min(Math.max(trimState.start + delta, 0), trimState.end - TRIM_MIN_SEC);
  } else {
    trimState.end = Math.min(Math.max(trimState.end + delta, trimState.start + TRIM_MIN_SEC), trimState.duration);
  }
  renderTrimTimeline();
}

for (const [handle, kind] of [[trimHandleStart, 'start'], [trimHandleEnd, 'end']]) {
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 1 : 0.1;
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      event.stopPropagation();
      nudgeTrim(kind, -step);
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      event.stopPropagation();
      nudgeTrim(kind, step);
    }
  });
}

// POST trim → 201 item mới: server đã append vào project, frontend chỉ mirror
// vào bản local + render. Lỗi 400/404/500 → toast detail, editor giữ nguyên.
videoEditorTrimSave.addEventListener('click', async () => {
  const state = trimState;
  if (!state || state.saving || !trimRangeValid()) return;
  state.saving = true;
  videoEditorTrimSave.disabled = true;
  const { start, end } = roundedTrimRange();
  try {
    const response = await request(
      projectPath(state.projectId, `/items/${encodeURIComponent(String(state.item.id))}/trim`),
      { method: 'POST', body: JSON.stringify({ start_sec: start, end_sec: end }) },
    );
    if (response.status === 401) {
      setSignedIn(false);
      return;
    }
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không cắt được video.');
      return;
    }
    const item = await response.json();
    mergeJobResults(state.projectId, [item]);
    if (state.projectId === activeProject) renderResultsGrid();
    showToast('Đã tạo bản trim.');
    closeVideoEditor();
  } catch {
    showToast('Không cắt được video.');
  } finally {
    if (trimState === state) {
      state.saving = false;
      videoEditorTrimSave.disabled = !trimRangeValid();
    }
  }
});

// ── Chế độ Extend trên composer (nối tiếp video) ────────────────────────────

function enterExtendMode(item) {
  extendState = {
    itemId: String(item.id),
    model: String(item.meta.model),
    ratio: String(item.meta.ratio),
    name: itemDisplayName(item, true),
  };
  genState.mode = 'video';
  genState.video.vmode = 'extend';
  genState.video.qty = 1; // backend: extend chỉ 1 kết quả mỗi lần
  genModelListOpen = false;
  extendChipLabel.textContent = `Nối tiếp: ${extendState.name}`;
  extendChip.hidden = false;
  renderGenDialog();
  renderComposerRefs();
  prompt.focus();
}

function exitExtendMode() {
  if (!extendState && genState.video.vmode !== 'extend') return;
  extendState = null;
  genState.video.vmode = 'text';
  extendChip.hidden = true;
  renderGenDialog();
  renderComposerRefs();
}

videoEditorExtend.addEventListener('click', () => {
  const item = trimState?.item;
  if (!item || !extendCapabilityFor(item).ok) return;
  enterExtendMode(item);
  closeVideoEditor();
});

extendChipCancel.addEventListener('click', () => exitExtendMode());

// ── Marquee selection (bôi vuông chọn item trong stage) ──────────────────
// Kéo chuột trái trên stage vẽ khung; thả ra → chọn mọi card giao khung.
// Ctrl/Cmd giữ khi kéo = chọn thêm (không bỏ selection cũ). Không giữ Ctrl =
// thay toàn bộ selection. Kéo < 4px coi như click thường (click không đụng).

const stage = document.querySelector('.stage');
const MARQUEE_THRESHOLD_PX = 4;

let marqueeState = null; // { box, startX, startY, additive, baseline:Set }

function marqueeRect(state) {
  const left = Math.min(state.startX, state.curX);
  const top = Math.min(state.startY, state.curY);
  return {
    left,
    top,
    width: Math.abs(state.curX - state.startX),
    height: Math.abs(state.curY - state.startY),
  };
}

function stageCardElements() {
  return [...resultsGrid.querySelectorAll('.result-card:not(.is-pending)')];
}

function rectsIntersect(a, b) {
  return !(b.right < a.left || b.left > a.right || b.bottom < a.top || b.top > a.bottom);
}

function marqueeHighlight(state) {
  const box = marqueeRect(state);
  const viewport = { left: box.left, top: box.top, right: box.left + box.width, bottom: box.top + box.height };
  for (const card of stageCardElements()) {
    const hit = rectsIntersect(viewport, card.getBoundingClientRect());
    card.classList.toggle('is-marquee', hit && !state.baseline.has(card.dataset.resultId));
  }
}

function onStagePointerDown(event) {
  // Chỉ chuột trái; bỏ qua nút/menu trên card và video controls.
  if (event.pointerType !== 'mouse' || event.button !== 0) return;
  const target = event.target;
  if (target instanceof Element && target.closest('button, a, input, video')) return;
  marqueeState = {
    startX: event.clientX,
    startY: event.clientY,
    curX: event.clientX,
    curY: event.clientY,
    additive: event.ctrlKey || event.metaKey,
    baseline: new Set(selectionIds()),
    moved: false,
    box: document.createElement('div'),
  };
  marqueeState.box.className = 'marquee-box';
  marqueeState.box.hidden = true;
  stage.append(marqueeState.box);
  event.preventDefault();
}

function onStagePointerMove(event) {
  if (!marqueeState) return;
  if ((event.buttons & 1) === 0) {
    endMarquee(false);
    return;
  }
  marqueeState.curX = event.clientX;
  marqueeState.curY = event.clientY;
  if (
    !marqueeState.moved &&
    Math.hypot(marqueeState.curX - marqueeState.startX, marqueeState.curY - marqueeState.startY) >
      MARQUEE_THRESHOLD_PX
  ) {
    marqueeState.moved = true;
    marqueeState.box.hidden = false;
  }
  if (marqueeState.moved) {
    const box = marqueeRect(marqueeState);
    Object.assign(marqueeState.box.style, {
      left: `${box.left}px`,
      top: `${box.top}px`,
      width: `${box.width}px`,
      height: `${box.height}px`,
    });
    marqueeHighlight(marqueeState);
  }
}

function onStagePointerUp(event) {
  if (!marqueeState) return;
  const state = marqueeState;
  marqueeState = null;
  state.box.remove();
  for (const card of stageCardElements()) card.classList.remove('is-marquee');
  if (!state.moved) {
    // Click thường trên vùng trống của stage = bỏ hết selection.
    if (!(event.target instanceof Element && event.target.closest('.result-card'))) {
      clearSelection();
    }
    return;
  }
  const viewport = (() => {
    const box = marqueeRect(state);
    return { left: box.left, top: box.top, right: box.left + box.width, bottom: box.top + box.height };
  })();
  const hitIds = stageCardElements()
    .filter((card) => rectsIntersect(viewport, card.getBoundingClientRect()))
    .map((card) => card.dataset.resultId)
    .filter(Boolean);
  const baseline = state.additive ? [...state.baseline] : [];
  const merged = [...baseline];
  for (const id of hitIds) {
    if (!merged.includes(id)) merged.push(id);
  }
  selectionByProject.set(activeProject, merged);
  selectionChanged();
}

function endMarquee(cancelled = true) {
  if (!marqueeState) return;
  marqueeState.box.remove();
  for (const card of stageCardElements()) card.classList.remove('is-marquee');
  marqueeState = null;
  if (cancelled) { /* giữ nguyên selection */ }
}

stage.addEventListener('pointerdown', onStagePointerDown);
window.addEventListener('pointermove', onStagePointerMove, { passive: true });
window.addEventListener('pointerup', onStagePointerUp);
stage.addEventListener('dragstart', (event) => event.preventDefault());

// --- Generate job engine ----------------------------------------------------
// Mỗi job là một entry trong jobsById (khoá theo jobId); poller riêng cho từng
// job trong timersByJob. Nguồn sự thật lúc poll/reconcile là GET /jobs của
// project (contract backend): {"jobs": [{job_id, status, error, results, created_at}]}.

function stopPolling(jobId) {
  const timer = timersByJob.get(jobId);
  if (timer) clearInterval(timer);
  timersByJob.delete(jobId);
}

function startPolling(jobId) {
  stopPolling(jobId);
  const timer = setInterval(() => { pollJob(jobId); }, 1500);
  timersByJob.set(jobId, timer);
}

function resumePollingIfNeeded(projectId) {
  // Job sống qua đổi project: khi project trở lại active mà chưa có poller nào
  // theo dõi thì dựng lại interval (sau F5 thì reconcileProjectJobs lo).
  // Job lỗi không cần poller — nó chờ user bấm Retry/Delete.
  if (!projectId) return;
  for (const [jobId, job] of jobsById) {
    if (job.projectId !== projectId || jobId.startsWith('tmp:')) continue;
    if (job.status === 'error') continue;
    if (!timersByJob.has(jobId)) startPolling(jobId);
  }
}

function persistJobQueue() {
  // Mirror queue vào localStorage: chỉ job thật (đã có job_id từ server) —
  // entry tmp:* đang POST chưa chắc tồn tại nên không lưu.
  const entries = [...jobsById.values()]
    .filter((job) => !String(job.jobId).startsWith('tmp:'))
    .map(({ projectId, jobId, mode, ratio, count, startedAt }) => ({
      projectId, jobId, mode, ratio, count, startedAt,
    }));
  try { localStorage.setItem(JOB_QUEUE_KEY, JSON.stringify(entries)); } catch { /* Storage may be unavailable. */ }
}

function loadJobQueue() {
  try {
    const parsed = JSON.parse(localStorage.getItem(JOB_QUEUE_KEY) ?? '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry) => entry
      && typeof entry.jobId === 'string' && entry.jobId
      && typeof entry.projectId === 'string' && entry.projectId
      && typeof entry.mode === 'string');
  } catch {
    return []; // Storage hỏng/thiếu — coi như queue rỗng, reconcile sẽ dựng lại.
  }
}

function removeJob(jobId) {
  jobsById.delete(jobId);
  stopPolling(jobId);
  persistJobQueue();
}

function clearProjectJobs(projectId) {
  for (const [jobId, job] of [...jobsById]) {
    if (job.projectId === projectId) removeJob(jobId);
  }
}

function clearAllJobsLocal() {
  // Chỉ dọn in-memory (logout/session hết hạn); queue localStorage giữ nguyên —
  // sign-in lại hoặc F5 sẽ reconcile theo server và khôi phục pending card.
  for (const jobId of [...timersByJob.keys()]) stopPolling(jobId);
  jobsById.clear();
}

// Video jobs can run for minutes; no stop shorter than the contract's 10 minutes.
const JOB_MAX_WAIT_MS = 20 * 60 * 1000;

async function fetchProjectJobs(projectId) {
  // GET /jobs theo contract backend; dedupe request đang bay giữa các poller
  // của cùng project để N job không sinh ra N request cùng lúc.
  const inFlight = jobsFetchInFlight.get(projectId);
  if (inFlight) return inFlight;
  const promise = (async () => {
    try {
      const response = await request(projectPath(projectId, '/jobs'));
      if (response.status === 401) return { unauthorized: true, jobs: [] };
      if (!response.ok) return null;
      const data = await response.json();
      return { jobs: Array.isArray(data?.jobs) ? data.jobs : [] };
    } catch {
      return null; // network hiccup: caller keeps polling
    } finally {
      jobsFetchInFlight.delete(projectId);
    }
  })();
  jobsFetchInFlight.set(projectId, promise);
  return promise;
}

async function pollJob(jobId) {
  const job = jobsById.get(jobId);
  if (!job) {
    stopPolling(jobId);
    return;
  }
  if (Date.now() - job.startedAt > JOB_MAX_WAIT_MS) {
    failJob(jobId, 'Hết thời gian chờ tạo nội dung.');
    return;
  }
  const data = await fetchProjectJobs(job.projectId);
  if (!data) return; // transient failure: keep polling
  if (data.unauthorized) {
    // Session hết hạn giữa chừng: poll thêm cũng vô ích — về key screen
    // thay vì chờ hết 20 phút rồi báo sai "hết thời gian".
    clearAllJobsLocal();
    setSignedIn(false);
    return;
  }
  reconcileJobsFromList(job.projectId, data.jobs, { live: true });
}

// Không toast rời rạc khi merge kết quả cũ lúc load — gộp 1 toast sau debounce.
let loadMergedCount = 0;
let loadMergedTimer = null;

function noteLoadMerge(count) {
  if (!count) return;
  loadMergedCount += count;
  clearTimeout(loadMergedTimer);
  loadMergedTimer = setTimeout(() => {
    const total = loadMergedCount;
    loadMergedCount = 0;
    if (total > 0) showToast(`Đã nạp ${total} kết quả mới từ các job đã xong.`);
  }, 800);
}

function guessJobEntry(projectId, jobId, server, results) {
  // Job running trên server nhưng không có trong queue (tab/device khác hoặc
  // storage mất): dựng entry từ dữ liệu liệt kê để vẫn hiện pending card.
  const project = projects.find(({ id }) => id === projectId);
  const projectResults = Array.isArray(project?.results) ? project.results : [];
  const lastProjectItem = [...projectResults].reverse().find(
    (item) => typeof item?.meta?.ratio === 'string' && /^\d+:\d+$/.test(item.meta.ratio),
  );
  const mode = results.some((item) => item?.kind === 'video')
    ? 'video'
    : (results.length === 0 && lastProjectItem?.kind === 'video')
      ? 'video'
      : 'image';
  const first = results.find((item) => typeof item?.meta?.ratio === 'string') ?? null;
  const ratio = typeof first?.meta?.ratio === 'string' && /^\d+:\d+$/.test(first.meta.ratio)
    ? first.meta.ratio
    : lastProjectItem?.meta?.ratio ?? (mode === 'video' ? '16:9' : '1:1');
  return {
    projectId,
    jobId,
    mode,
    ratio,
    count: Math.max(results.length, 1),
    startedAt: typeof server.created_at === 'number' ? server.created_at : Date.now(),
  };
}

function reconcileJobsFromList(projectId, serverJobs, { live = false } = {}) {
  const serverById = new Map();
  for (const entry of serverJobs) {
    if (entry && typeof entry.job_id === 'string') serverById.set(entry.job_id, entry);
  }
  let gridDirty = false;
  for (const [jobId, job] of [...jobsById]) {
    if (job.projectId !== projectId || jobId.startsWith('tmp:')) continue;
    const server = serverById.get(jobId);
    if (!server) {
      // Job lỗi biến mất khỏi list = server đã dọn (quá 24h hoặc vượt 20 job)
      // — thẻ lỗi không còn hậu thuẫn, gỡ khỏi queue luôn.
      if (job.status === 'error') {
        removeJob(jobId);
        gridDirty = true;
      }
      continue; // chưa thấy trong danh sách — giữ nguyên chờ poll sau
    }
    if (server.status === 'done') {
      const outcome = finishJob(jobId, Array.isArray(server.results) ? server.results : []);
      if (outcome) {
        if (live) {
          const noun = outcome.mode === 'video' ? 'video' : 'ảnh';
          if (outcome.created < outcome.count) {
            showToast(`Đã tạo ${outcome.created}/${outcome.count} ${noun} thành công.`);
          } else {
            showToast(`Đã tạo ${outcome.created} ${noun}.`);
          }
        } else {
          noteLoadMerge(outcome.merged); // load: gộp 1 toast, không toast từng job
        }
        gridDirty = true;
      }
    } else if (server.status === 'error') {
      // Job lỗi giữ thẻ tại chỗ (Retry/Delete); đang live thì báo thêm toast.
      failJob(jobId, server.error || 'Không tạo được nội dung.', { silent: !live });
      gridDirty = true;
    } else if (server.status === 'running') {
      // Still running: adopt partial results early when the backend streams them.
      // (Job lỗi local mà server báo running = tab khác vừa retry — hồi spinner.)
      if (job.status === 'error') {
        job.status = 'running';
        delete job.error;
        gridDirty = true;
      }
      const results = Array.isArray(server.results) ? server.results : [];
      const prev = Array.isArray(job.partial) ? job.partial : [];
      if (results.length !== prev.length) gridDirty = true;
      job.partial = results;
      if (!timersByJob.has(jobId)) {
        startPolling(jobId);
        gridDirty = true;
      }
    }
  }
  if (!live) {
    // Lượt reconcile lúc load/đổi project: track thêm job running + error chưa có
    // trong jobsById — ưu tiên metadata từ queue localStorage (đúng mode/count/
    // ratio lúc gửi), thiếu thì đoán từ results. Entry done còn trong queue là
    // job xong lúc trang đóng: merge tĩnh (dedupe theo id) + dọn khỏi queue;
    // entry error thì dựng thẻ lỗi (giữ trong queue tới khi user retry/delete).
    const queueEntries = new Map(loadJobQueue().map((entry) => [entry.jobId, entry]));
    // POST đang bay cho project này thì job của nó sẽ có entry thật khi POST
    // trả job_id — adopt bây giờ sẽ sinh pending card trùng, nên bỏ qua lượt này.
    const hasInFlightPost = [...jobsById.values()].some(
      (job) => job.projectId === projectId && String(job.jobId).startsWith('tmp:'),
    );
    for (const [serverId, server] of serverById) {
      if (jobsById.has(serverId)) continue;
      const results = Array.isArray(server.results) ? server.results : [];
      if (server.status === 'running') {
        if (hasInFlightPost) continue; // chỉ bỏ qua adopt running — done/error vẫn dọn queue
        const queued = queueEntries.get(serverId);
        const entry = queued ?? guessJobEntry(projectId, serverId, server, results);
        jobsById.set(serverId, {
          ...entry,
          count: Math.max(1, Number(entry.count) || 1),
          startedAt: Number(entry.startedAt) || Date.now(),
          partial: results,
        });
        startPolling(serverId);
        gridDirty = true;
      } else if (server.status === 'error') {
        // Job lỗi chưa có trong jobsById (tab khác gửi hoặc queue mất): dựng
        // thẻ lỗi để user vẫn Retry/Delete được sau F5.
        const queued = queueEntries.get(serverId);
        const entry = queued ?? guessJobEntry(projectId, serverId, server, results);
        jobsById.set(serverId, {
          ...entry,
          count: Math.max(1, Number(entry.count) || 1),
          startedAt: Number(entry.startedAt) || Date.now(),
          status: 'error',
          error: server.error || 'Không tạo được nội dung.',
          partial: [],
        });
        gridDirty = true;
      } else if (queueEntries.has(serverId)) {
        // Job done còn trong queue: merge tĩnh kết quả rồi dọn entry.
        const merged = mergeJobResults(projectId, results);
        if (merged) noteLoadMerge(merged);
      }
    }
    // Mirror hiện trạng: entry stale (done/project đã xóa) rơi khỏi queue;
    // job lỗi giữ nguyên trong queue cho tới khi user retry/delete.
    persistJobQueue();
  }
  if (gridDirty && projectId === activeProject) renderResultsGrid();
}

async function reconcileProjectJobs(projectId) {
  // Gọi sau sign-in + nạp projects và mỗi lần đổi project active.
  if (!projectId) return;
  const data = await fetchProjectJobs(projectId);
  if (!data) return;
  if (data.unauthorized) {
    clearAllJobsLocal();
    setSignedIn(false);
    return;
  }
  reconcileJobsFromList(projectId, data.jobs, { live: false });
}

function reconcileQueuedProjects() {
  // Sau sign-in: mọi project còn job trong queue localStorage đều được đối chiếu.
  for (const projectId of new Set(loadJobQueue().map((entry) => entry.projectId))) {
    reconcileProjectJobs(projectId);
  }
}

function mergeJobResults(projectId, results) {
  const project = projects.find(({ id }) => id === projectId);
  if (!project || !Array.isArray(results) || !results.length) return 0;
  const existing = Array.isArray(project.results) ? project.results : [];
  const seen = new Set(existing.map((item) => item?.id).filter((id) => id !== undefined && id !== null));
  const fresh = results.filter((item) => item?.id === undefined || item?.id === null || !seen.has(item.id));
  if (!fresh.length) return 0;
  project.results = [...existing, ...fresh];
  project.updated_at = new Date().toISOString();
  sortProjects();
  renderProjects();
  return fresh.length;
}

function finishJob(jobId, results) {
  const job = jobsById.get(jobId);
  if (!job) return null;
  const created = Array.isArray(results) ? results.length : 0;
  removeJob(jobId);
  // Server đã append results; mirror vào bản copy local (dedupe theo id nên an
  // toàn kể cả khi results đã nằm sẵn trong project sau F5).
  const merged = mergeJobResults(job.projectId, results);
  if (job.projectId === activeProject) {
    renderResultsGrid();
  }
  return { created, merged, mode: job.mode, count: job.count };
}

function failJob(jobId, message, { silent = false } = {}) {
  // Job lỗi KHÔNG bị gỡ khỏi queue nữa — giữ thẻ tại chỗ kèm nút Retry/Delete.
  // Server giữ row lỗi 24h nên sau F5 reconcile vẫn dựng lại thẻ từ GET /jobs.
  const job = jobsById.get(jobId);
  if (!job) return;
  stopPolling(jobId);
  job.status = 'error';
  job.partial = [];
  if (message) job.error = String(message);
  if (message && !silent) showToast(String(message));
  if (job.projectId === activeProject) renderResultsGrid();
}

async function retryFailedJob(jobId) {
  const job = jobsById.get(jobId);
  if (!job || job.status !== 'error') return;
  let response;
  try {
    response = await request(
      projectPath(job.projectId, `/jobs/${encodeURIComponent(jobId)}/retry`),
      { method: 'POST' },
    );
  } catch {
    showToast('Không kết nối được với máy chủ.');
    return;
  }
  if (response.status === 401 || response.status === 403) {
    setSignedIn(false);
    keyInput.focus();
    return;
  }
  if (!response.ok) {
    // 409 (tham số gốc không còn hợp lệ / job không còn trạng thái lỗi), 429…
    let message = 'Không thử lại được job này.';
    try {
      const data = await response.json();
      if (data?.detail) message = String(data.detail);
    } catch { /* Keep the generic message. */ }
    showToast(message);
    return;
  }
  // Server đã reset job về running (giữ job_id) — thẻ lỗi biến lại spinner.
  job.status = 'running';
  delete job.error;
  job.partial = [];
  job.startedAt = Date.now(); // reset đồng hồ JOB_MAX_WAIT_MS + đưa lên đầu queue
  startPolling(jobId);
  persistJobQueue();
  if (job.projectId === activeProject) renderResultsGrid();
}

async function deleteFailedJob(jobId) {
  const job = jobsById.get(jobId);
  if (!job) return;
  let response;
  try {
    response = await request(
      projectPath(job.projectId, `/jobs/${encodeURIComponent(jobId)}`),
      { method: 'DELETE' },
    );
  } catch {
    showToast('Không kết nối được với máy chủ.');
    return;
  }
  if (response.status === 401 || response.status === 403) {
    setSignedIn(false);
    keyInput.focus();
    return;
  }
  if (!response.ok) {
    let message = 'Không xóa được job này.';
    try {
      const data = await response.json();
      if (data?.detail) message = String(data.detail);
    } catch { /* Keep the generic message. */ }
    showToast(message);
    return;
  }
  removeJob(jobId);
  persistJobQueue();
  if (job.projectId === activeProject) renderResultsGrid();
}

function showError(message = '', invalidField = null) {
  loginError.hidden = !message;
  loginError.textContent = message;
  for (const input of [urlInput, keyInput]) {
    if (invalidField && input === (invalidField === 'url' ? urlInput : keyInput)) {
      input.setAttribute('aria-invalid', 'true');
      input.setAttribute('aria-describedby', 'login-error');
    } else {
      input.removeAttribute('aria-invalid');
      input.removeAttribute('aria-describedby');
    }
  }
}

function focusLoginField() {
  // Ưu tiên ô còn trống — URL thường được giữ lại sau lần đăng nhập trước.
  (!urlInput.value ? urlInput : keyInput).focus();
}

function setSignedIn(signedIn) {
  login.hidden = signedIn;
  workspace.hidden = !signedIn;
  logout.hidden = !signedIn;
  wmSettingsButton.hidden = !signedIn;
  densityPicker.hidden = !signedIn;
  if (signedIn) {
    prompt.focus();
  } else {
    projects = [];
    activeProject = null;
    GEN_CAPS = null;
    vmodeManual = false;
    frameSlotsByProject.clear();
    closeFramePicker();
    clearDropHighlights();
    clearAllJobsLocal(); // giữ queue localStorage — sign-in lại sẽ reconcile tiếp
    closeProjectActions();
    closeVideoEditor();
    closeLightbox();
    exitExtendMode();
    buildStaticRows();
    renderGenDialog();
    renderProjects();
    showProjectContents();
    sidebarToggle.setAttribute('aria-expanded', 'false');
  }
}

async function restoreSession() {
  try {
    const response = await request('/playground/session');
    if (!response.ok) return;
    setSignedIn(true);
    await Promise.all([loadGenOptions(), loadProjects()]);
    // Khôi phục queue job qua F5: mọi project còn entry trong localStorage.
    reconcileQueuedProjects();
  } catch {
    // Leave the key-entry screen available while offline.
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (loginButton.disabled) return;
  const apiUrl = urlInput.value.trim().replace(/\/+$/, '');
  const key = keyInput.value.trim();
  if (!/^https?:\/\/[^\s]+/i.test(apiUrl)) {
    showError('API URL must start with http:// or https://.', 'url');
    return;
  }
  if (!key || /[^\x21-\x7e]/.test(key)) {
    showError('Invalid API key.', 'key');
    return;
  }
  showError();
  loginButton.disabled = true;
  urlInput.readOnly = true;
  keyInput.readOnly = true;
  loginButton.textContent = 'Verifying…';
  try {
    // Endpoint + key được server kiểm tra cùng lúc với endpoint đã nhập —
    // đúng cả hai mới cấp session.
    const response = await request('/playground/session', {
      method: 'POST',
      body: JSON.stringify({ api_url: apiUrl, api_key: key }),
    });
    if (response.status === 204) {
      keyInput.value = '';
      setSignedIn(true);
      await Promise.all([loadGenOptions(), loadProjects()]);
      reconcileQueuedProjects();
    } else {
      const detail = await response.json().then((data) => data?.detail).catch(() => null);
      if (response.status === 502) {
        showError(detail || 'Could not connect to the API URL — check the endpoint address.', 'url');
      } else if (response.status === 422) {
        showError(detail || 'Invalid API URL.', 'url');
      } else if (response.status === 401 || response.status === 403) {
        showError(detail || 'Invalid API key for this endpoint.', 'key');
      } else {
        showError('Could not authenticate. Please try again.');
      }
    }
  } catch {
    showError('Could not reach the server. Please try again.');
  } finally {
    loginButton.disabled = false;
    urlInput.readOnly = false;
    keyInput.readOnly = false;
    loginButton.textContent = 'Sign in';
    if (!login.hidden) focusLoginField();
  }
});

urlInput.addEventListener('input', () => showError());
keyInput.addEventListener('input', () => showError());

function autoGrowPrompt() {
  const style = getComputedStyle(prompt);
  const lineHeight = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.45;
  const padY = (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0);
  const maxHeight = lineHeight * 6 + padY;
  prompt.style.height = 'auto';
  prompt.style.height = `${Math.min(prompt.scrollHeight, maxHeight)}px`;
  prompt.style.overflowY = prompt.scrollHeight > maxHeight ? 'auto' : 'hidden';
}

prompt.addEventListener('input', () => {
  autoGrowPrompt();
  syncComposerState();
  clearTimeout(promptSaveTimer);
  promptSaveTimer = setTimeout(async () => {
    const active = projects.find(({ id }) => id === activeProject);
    if (!active) return;
    try {
      const response = await request(projectPath(active.id), {
        method: 'PATCH',
        body: JSON.stringify({ prompt: prompt.value }),
      });
      if (!response.ok) throw new Error('Could not save prompt');
      const updated = parseProject(await response.json());
      if (updated) {
        projects = [updated, ...projects.filter(({ id }) => id !== updated.id)];
        sortProjects();
        // KHÔNG renderProjects() ở đây: re-render sidebar vừa giật focus khỏi
        // textarea (autosave chạy 0.5s sau khi ngừng gõ), thứ tự sidebar sẽ
        // tự cập nhật ở lần render kế tiếp.
      }
    } catch {
      showToast('Chưa thể lưu nội dung project.');
    }
  }, 500);
});

// Tên project mặc định theo format Google Flow: "M d - hh:ii" (vd "Nov 23 - 14:30").
// Tháng tiếng Anh viết tắt, ngày không số 0 đầu, giờ:phút 24h. Trùng tên đang có
// thì thêm hậu tố " (2)", " (3)"…
const MONTH_LABELS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function defaultProjectName() {
  const now = new Date();
  const base = `${MONTH_LABELS[now.getMonth()]} ${now.getDate()} - ${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const names = new Set(projects.map(({ name }) => name));
  if (!names.has(base)) return base;
  let suffix = 2;
  while (names.has(`${base} (${suffix})`)) suffix += 1;
  return `${base} (${suffix})`;
}

newProject.addEventListener('click', async () => {
  try {
    const response = await request('/playground/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name: defaultProjectName(), prompt: '', results: [] }),
    });
    if (!response.ok) throw new Error('Project creation failed');
    const project = parseProject(await response.json());
    if (!project) throw new Error('Invalid project response');
    projects = [project, ...projects.filter(({ id }) => id !== project.id)];
    sortProjects();
    activeProject = project.id;
    renderProjects(project.id);
    showProjectContents();
  } catch {
    showToast('Chưa thể tạo project.');
  }
});

sidebarToggle.addEventListener('click', () => {
  const expanded = sidebarToggle.getAttribute('aria-expanded') === 'true';
  sidebarToggle.setAttribute('aria-expanded', String(!expanded));
  if (expanded) closeProjectActions();
});

function applyResultColumns(count, persist = false) {
  resultColumns = count;
  document.documentElement.style.setProperty('--result-columns', String(count));
  for (const button of densityOptions) {
    const selected = Number(button.dataset.columns) === count;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  }
  if (persist) {
    try { localStorage.setItem(COLUMNS_STORAGE_KEY, String(count)); } catch { /* Storage may be unavailable. */ }
  }
}

for (const option of densityOptions) {
  option.addEventListener('click', () => applyResultColumns(Number(option.dataset.columns), true));
}

(function restoreResultColumns() {
  try {
    const stored = Number(localStorage.getItem(COLUMNS_STORAGE_KEY));
    const valid = densityOptions.map((option) => Number(option.dataset.columns));
    if (valid.includes(stored)) applyResultColumns(stored);
  } catch { /* Storage may be unavailable. */ }
})();

function optionsFor(mode) {
  // Merged server capability wins; GEN_OPTIONS is the offline fallback.
  return GEN_CAPS?.[mode] ?? GEN_OPTIONS[mode];
}

function genSelectedModel(mode) {
  return (
    optionsFor(mode).models.find((model) => model.id === genState[mode].model) ??
    optionsFor(mode).models[0]
  );
}

function lengthSliderSpec(model) {
  // model.length is a discrete value list (e.g. [4, 6, 8]) — derive slider min/max/step from it.
  const values = [...model.length].sort((a, b) => a - b);
  const gaps = values.slice(1).map((value, index) => value - values[index]);
  const step = gaps.length && gaps.every((gap) => gap === gaps[0]) ? gaps[0] : 1;
  return { values, min: values[0], max: values[values.length - 1], step };
}

function snapToLength(values, current) {
  // Snap to the closest supported duration (ties favour the longer one).
  return values.reduce((best, value) =>
    Math.abs(value - current) <= Math.abs(best - current) ? value : best
  );
}

function applyModelCapability(mode) {
  // Clamp the current state to what the selected model actually supports.
  const model = genSelectedModel(mode);
  if (!model) return;
  const state = genState[mode];
  state.model = model.id;
  if (Array.isArray(model.ratios) && model.ratios.length && !model.ratios.includes(state.ratio)) {
    state.ratio = model.ratios[0];
  }
  if (Array.isArray(model.res) && model.res.length && !model.res.includes(state.res)) {
    state.res = model.res[0];
  }
  if (mode === 'video') {
    if (Array.isArray(model.length) && model.length.length) {
      const { values } = lengthSliderSpec(model);
      if (!values.includes(state.length)) state.length = snapToLength(values, state.length);
    }
    if (state.vmode === 'frames' && model.frames !== true) {
      vmodeManual = false;
      state.vmode = model.refs === true && selectedImageItems().length > 0 ? 'ingredients' : 'text';
    } else if (state.vmode === 'ingredients' && model.refs !== true) {
      state.vmode = 'text';
    }
  }
}

function defaultModelIcon(mode, model) {
  if (typeof model.icon === 'string' && model.icon) return model.icon;
  if (mode === 'image') {
    const id = String(model.id || '').toLowerCase();
    const base = String(model.base || '').toLowerCase();
    if (id.includes('nano-banana') || base.includes('gemini-3') || base.includes('gemini-2.5')) {
      return '🍌';
    }
  }
  return undefined;
}

function mergeCapabilities(data) {
  const merged = {};
  for (const mode of ['image', 'video']) {
    const hasServerModels = Array.isArray(data?.[mode]?.models);
    const serverModels = hasServerModels ? data[mode].models : GEN_OPTIONS[mode].models;
    const staticById = new Map(GEN_OPTIONS[mode].models.map((m) => [m.id, m]));
    const models = serverModels
      .filter((server) => server && typeof server.id === 'string' && server.id)
      .map((server) => {
        const fallback = staticById.get(server.id);
        const icon = defaultModelIcon(mode, server) ?? fallback?.icon;
        return {
          ...(fallback ?? {}),
          ...server,
          ...(icon ? { icon } : {}),
          label: typeof server.label === 'string' && server.label ? server.label : (fallback?.label ?? server.id),
          base: typeof server.base === 'string' && server.base ? server.base : (fallback?.base ?? server.id),
          ratios: Array.isArray(server.ratios) ? server.ratios : (fallback?.ratios ?? GEN_OPTIONS[mode].ratios),
        };
      });
    merged[mode] = { models };
  }
  GEN_CAPS = merged;
  if (!merged[genState.mode].models.length) {
    const other = genState.mode === 'image' ? 'video' : 'image';
    if (merged[other].models.length) genState.mode = other;
  }
  for (const mode of ['image', 'video']) applyModelCapability(mode);
  buildStaticRows();
  renderGenDialog();
  renderComposerRefs();
}

async function loadGenOptions() {
  try {
    const response = await request('/playground/api/gen-options');
    if (!response.ok) return;
    mergeCapabilities(await response.json());
  } catch {
    // Keep the static capability; options must never block the UI.
  }
}

function ratioShapeIcon(ratio) {
  const [width, height] = ratio.split(':').map(Number);
  const scale = Math.min(16 / width, 15 / height);
  const boxWidth = width * scale;
  const boxHeight = height * scale;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('width', '18');
  svg.setAttribute('height', '18');
  svg.setAttribute('viewBox', '0 0 18 18');
  svg.setAttribute('aria-hidden', 'true');
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('x', String((18 - boxWidth) / 2));
  rect.setAttribute('y', String((18 - boxHeight) / 2));
  rect.setAttribute('width', String(boxWidth));
  rect.setAttribute('height', String(boxHeight));
  rect.setAttribute('rx', '2.5');
  rect.setAttribute('fill', 'none');
  rect.setAttribute('stroke', 'currentColor');
  rect.setAttribute('stroke-width', '1.5');
  svg.append(rect);
  return svg;
}

function modelIconSpan(model) {
  const icon = document.createElement('span');
  icon.className = 'gen-model-icon';
  icon.textContent = model.icon;
  return icon;
}

function buildStaticRows() {
  for (const mode of ['image', 'video']) {
    const ratioSet = [...GEN_OPTIONS[mode].ratios];
    for (const model of optionsFor(mode)?.models ?? []) {
      for (const ratio of model?.ratios ?? []) {
        if (!ratioSet.includes(ratio)) ratioSet.push(ratio);
      }
    }
    genRatioRows[mode].replaceChildren(
      ...ratioSet.map((ratio) => {
        const button = makeButton('gen-ratio', '');
        button.dataset.ratio = ratio;
        button.setAttribute('aria-label', `Tỉ lệ ${ratio}`);
        const label = document.createElement('span');
        label.textContent = ratio;
        button.append(ratioShapeIcon(ratio), label);
        button.addEventListener('click', () => {
          if (button.classList.contains('is-disabled')) return;
          genState[mode].ratio = ratio;
          renderGenDialog();
        });
        return button;
      })
    );
    genQtyRows[mode].replaceChildren(
      ...[1, 2, 3, 4].map((qty) => {
        const button = makeButton('gen-qty-btn', `x${qty}`);
        button.dataset.qty = String(qty);
        button.addEventListener('click', () => {
          genState[mode].qty = qty;
          renderGenDialog();
        });
        return button;
      })
    );
  }
}

function renderModelRow(mode) {
  const button = genModelButtons[mode];
  button.replaceChildren();
  // Extend mode: khóa model theo video gốc; id không còn trong catalog vẫn
  // hiện label gốc dạng text (backend skip spec_entry khi vmode extend).
  if (mode === 'video' && genState.video.vmode === 'extend' && extendState) {
    button.disabled = true;
    button.title = 'Đang nối tiếp — model giữ theo video gốc';
    const name = document.createElement('span');
    name.className = 'gen-model-name';
    name.textContent = modelLabelForId('video', extendState.model) ?? extendState.model;
    button.append(name);
    return;
  }
  const model = genSelectedModel(mode);
  if (!model) {
    button.disabled = true;
    button.title = 'API key hiện tại không có model cho chế độ này';
    const name = document.createElement('span');
    name.className = 'gen-model-name';
    name.textContent = 'Không có model khả dụng';
    button.append(name);
    return;
  }
  button.disabled = false;
  button.removeAttribute('title');
  if (model.icon) button.append(modelIconSpan(model));
  const name = document.createElement('span');
  name.className = 'gen-model-name';
  name.textContent = model.label;
  const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chevron.setAttribute('class', 'gen-model-chevron');
  chevron.setAttribute('width', '14');
  chevron.setAttribute('height', '14');
  chevron.setAttribute('viewBox', '0 0 24 24');
  chevron.setAttribute('fill', 'none');
  chevron.setAttribute('stroke', 'currentColor');
  chevron.setAttribute('stroke-width', '2');
  chevron.setAttribute('stroke-linecap', 'round');
  chevron.setAttribute('stroke-linejoin', 'round');
  chevron.setAttribute('aria-hidden', 'true');
  const chevronPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  chevronPath.setAttribute('d', 'm6 9 6 6 6-6');
  chevron.append(chevronPath);
  button.append(name, chevron);
}

function renderModelList() {
  const mode = genState.mode;
  genModelList.replaceChildren(
    ...optionsFor(mode).models.map((model) => {
      const option = makeButton('gen-model-option', '');
      if (model.icon) option.append(modelIconSpan(model));
      const name = document.createElement('span');
      name.className = 'gen-model-name';
      name.textContent = model.label;
      option.append(name);
      if (model.id === genState[mode].model) {
        const check = document.createElement('span');
        check.className = 'gen-model-option-check';
        check.textContent = '✓';
        option.append(check);
      }
      option.addEventListener('click', () => {
        genState[mode].model = model.id;
        applyModelCapability(mode);
        genModelListOpen = false;
        renderGenDialog();
        renderComposerRefs();
      });
      return option;
    })
  );
}

function renderResRow() {
  // Extend không dùng res — ẩn hàng để khớp validate backend.
  if (genState.video.vmode === 'extend') {
    genResRow.hidden = true;
    return;
  }
  const resolutions = genSelectedModel('video')?.res;
  if (!resolutions) {
    genResRow.hidden = true;
    return;
  }
  genResRow.replaceChildren(
    ...resolutions.map((value) => {
      const button = makeButton('gen-qty-btn', value);
      button.dataset.res = value;
      button.addEventListener('click', () => {
        genState.video.res = value;
        renderGenDialog();
      });
      return button;
    })
  );
  genResRow.hidden = false;
}

function renderResImageRow() {
  const resolutions = genSelectedModel('image')?.res;
  if (!resolutions?.length) {
    genResImageRow.hidden = true;
    return;
  }
  genResImageRow.replaceChildren(
    ...resolutions.map((value) => {
      const button = makeButton('gen-qty-btn', value);
      button.dataset.res = value;
      button.addEventListener('click', () => {
        genState.image.res = value;
        renderGenDialog();
      });
      return button;
    })
  );
  genResImageRow.hidden = false;
}

function lengthTickValues(values) {
  if (values.length <= 7) return values;
  const min = values[0];
  const max = values[values.length - 1];
  const picked = new Set([min, max]);
  const span = max - min;
  const stride = span >= 10 ? 3 : 2;
  for (let v = min; v < max; v += 1) {
    if (values.includes(v) && (v - min) % stride === 0) picked.add(v);
  }
  return [...picked].sort((a, b) => a - b);
}

function renderLengthRow() {
  // Extend không dùng length — ẩn slider để khớp validate backend.
  if (genState.video.vmode === 'extend') {
    genLengthRow.hidden = true;
    return;
  }
  const model = genSelectedModel('video');
  if (!model || !Array.isArray(model.length) || !model.length.length) {
    genLengthRow.hidden = true;
    return;
  }
  const { values, min, max, step } = lengthSliderSpec(model);
  if (!values.includes(genState.video.length)) {
    genState.video.length = snapToLength(values, genState.video.length);
  }
  genLengthSlider.min = String(min);
  genLengthSlider.max = String(max);
  genLengthSlider.step = String(step);
  genLengthSlider.value = String(genState.video.length);
  genLengthValue.textContent = `${genState.video.length} Sec`;
  // Tick labels under the track: discrete ticks when compact (e.g. 4s/6s/8s), evenly spaced for wide ranges (3..15s).
  const tickValues = lengthTickValues(values);
  genLengthTicks.replaceChildren(
    ...tickValues.map((value) => {
      const tick = document.createElement('span');
      tick.className = 'gen-length-tick';
      tick.dataset.value = String(value);
      tick.textContent = `${value}s`;
      tick.style.setProperty('--tick-pos', String(max > min ? (value - min) / (max - min) : 0));
      tick.classList.toggle('is-active', value === genState.video.length);
      return tick;
    })
  );
  genLengthRow.hidden = false;
}

function renderGenDialog() {
  for (const tab of genTabs) {
    const active = tab.dataset.mode === genState.mode;
    tab.classList.toggle('is-active', active);
    tab.setAttribute('aria-selected', String(active));
  }
  genBodies.image.hidden = genState.mode !== 'image';
  genBodies.video.hidden = genState.mode !== 'video';
  for (const mode of ['image', 'video']) {
    const allowedRatios = genSelectedModel(mode)?.ratios;
    // Extend mode: ratio/qty bị khóa theo video gốc (chỉ render, không đụng
    // genState để thoát extend trả lại đúng lựa chọn tự do trước đó).
    const extendLocked = mode === 'video' && genState.video.vmode === 'extend';
    const activeRatio = extendLocked ? extendState?.ratio : genState[mode].ratio;
    for (const button of genRatioRows[mode].children) {
      const supported = !Array.isArray(allowedRatios) || allowedRatios.includes(button.dataset.ratio);
      button.classList.toggle('is-disabled', !supported || extendLocked);
      const active = button.dataset.ratio === activeRatio;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
      if (extendLocked) {
        button.disabled = true;
        button.title = 'Đang nối tiếp — tỉ lệ giữ theo video gốc';
      } else {
        button.disabled = false;
        if (!supported) {
          button.setAttribute('aria-disabled', 'true');
          button.title = 'Không hỗ trợ bởi model này';
        } else {
          button.removeAttribute('aria-disabled');
          button.removeAttribute('title');
        }
      }
    }
    for (const button of genQtyRows[mode].children) {
      const active = Number(button.dataset.qty) === genState[mode].qty;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
      if (extendLocked && button.dataset.qty !== '1') {
        button.disabled = true;
        button.title = 'Extend chỉ tạo 1 kết quả mỗi lần';
      } else {
        button.disabled = false;
        button.removeAttribute('title');
      }
    }
    renderModelRow(mode);
    genModelButtons[mode].setAttribute(
      'aria-expanded',
      String(genModelListOpen && genState.mode === mode)
    );
  }
  // Extend mode: cả 3 nút vmode bị khóa — thoát extend qua nút × trên chip.
  const extendLockedVideo = genState.mode === 'video' && genState.video.vmode === 'extend';
  for (const button of genVmodeRow.querySelectorAll('.gen-seg-btn')) {
    if (extendLockedVideo) {
      button.classList.remove('is-active');
      button.setAttribute('aria-pressed', 'false');
      button.classList.add('is-disabled');
      button.disabled = true;
      button.title = 'Đang ở chế độ nối tiếp — bấm × trên chip để hủy';
      button.setAttribute('aria-disabled', 'true');
      continue;
    }
    button.disabled = false;
    const active = button.dataset.vmode === genState.video.vmode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
    const videoModels = optionsFor('video')?.models ?? [];
    const videoModel = genSelectedModel('video');
    const anyFramesModel = videoModels.some((m) => m?.frames === true);
    const imageCount = selectedImageItems().length;
    let enabled = true;
    let hint = '';
    if (button.dataset.vmode === 'frames') {
      // Frames luôn bấm được khi có ít nhất 1 model video hỗ trợ Frames (dù chưa chọn ảnh).
      enabled = videoModel?.frames === true || anyFramesModel;
      hint = enabled
        ? 'Tạo video từ Start frame và End frame'
        : 'Không có model nào hỗ trợ Frames to Video';
    } else if (button.dataset.vmode === 'ingredients') {
      // Ingredients chỉ mở khi model hỗ trợ và có 1-3 ảnh đang được chọn.
      enabled = videoModel?.refs === true && imageCount >= 1 && imageCount <= 3;
      hint = videoModel?.refs === false
        ? 'Model này không hỗ trợ ảnh tham chiếu'
        : 'Bôi chọn 1-3 ảnh trong stage (Ctrl/Cmd + click).';
    }
    button.classList.toggle('is-disabled', !enabled);
    if (enabled) {
      button.removeAttribute('aria-disabled');
      button.removeAttribute('title');
    } else {
      button.setAttribute('aria-disabled', 'true');
      button.title = hint;
    }
  }
  renderResImageRow();
  if (!genResImageRow.hidden) {
    for (const button of genResImageRow.children) {
      const active = button.dataset.res === genState.image.res;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    }
  }
  renderResRow();
  if (!genResRow.hidden) {
    for (const button of genResRow.children) {
      const active = button.dataset.res === genState.video.res;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    }
  }
  renderLengthRow();
  genModelList.hidden = !genModelListOpen;
  if (genModelListOpen) {
    genModelButtons[genState.mode].after(genModelList);
    renderModelList();
  }
  updateGenTrigger();
}

function updateGenTrigger() {
  const state = genState[genState.mode];
  const model = genSelectedModel(genState.mode);
  genIconImage.hidden = genState.mode !== 'image';
  genIconVideo.hidden = genState.mode !== 'video';
  if (genState.mode === 'video' && genState.video.vmode === 'extend' && extendState) {
    // Extend: trigger hiển thị model + tỉ lệ của video gốc, không phải state tự do.
    const label = modelLabelForId('video', extendState.model) ?? extendState.model;
    genLabel.textContent = `${label} · ${extendState.ratio} · x1`;
    return;
  }
  if (!model) {
    genLabel.textContent = 'Không có model khả dụng';
    return;
  }
  const vmodeBadge = genState.mode === 'video' && genState.video.vmode !== 'text'
    ? ` · ${genState.video.vmode === 'frames' ? 'Frames' : 'Ingredients'}`
    : '';
  genLabel.textContent = `${model.label}${vmodeBadge} · ${state.ratio} · x${state.qty}`;
}

function setGenDialogOpen(open) {
  genDialog.hidden = !open;
  genTrigger.setAttribute('aria-expanded', String(open));
  genModelListOpen = false;
  if (open) closeFramePicker();
}

function setGenMode(mode) {
  if (!genBodies[mode]) return;
  // Rời tab video khi đang nối tiếp → hủy chế độ extend (chip + khóa).
  if (mode !== 'video' && genState.video.vmode === 'extend') exitExtendMode();
  genState.mode = mode;
  if (mode === 'video') applyVideoVmodeAuto();
  genModelListOpen = false;
  renderGenDialog();
  renderComposerRefs();
}

genTrigger.addEventListener('click', () => setGenDialogOpen(genDialog.hidden));
for (const tab of genTabs) {
  tab.addEventListener('click', () => setGenMode(tab.dataset.mode));
}
for (const button of genVmodeRow.querySelectorAll('.gen-seg-btn')) {
  button.addEventListener('click', () => {
    const videoModels = optionsFor('video')?.models ?? [];
    let videoModel = genSelectedModel('video');
    const imageCount = selectedImageItems().length;
    if (button.dataset.vmode === 'frames') {
      if (videoModel?.frames !== true) {
        const framesCapable = videoModels.find((m) => m?.frames === true);
        if (!framesCapable) {
          showToast('Không có model nào hỗ trợ Frames to Video.');
          return;
        }
        genState.video.model = framesCapable.id;
        applyModelCapability('video');
        videoModel = genSelectedModel('video');
        showToast(`Đã chuyển sang ${framesCapable.label} (hỗ trợ Frames).`);
      }
      vmodeManual = true;
    } else {
      vmodeManual = false;
    }
    if (button.dataset.vmode === 'ingredients') {
      if (videoModel?.refs !== true) {
        showToast('Model này không hỗ trợ ảnh tham chiếu.');
        return;
      }
      if (imageCount < 1 || imageCount > 3) {
        showToast('Chọn 1-3 ảnh tham chiếu trong stage (Ctrl/Cmd + click).');
        return;
      }
    }
    genState.video.vmode = button.dataset.vmode;
    renderGenDialog();
    renderComposerRefs();
  });
}
for (const mode of ['image', 'video']) {
  genModelButtons[mode].addEventListener('click', () => {
    genModelListOpen = !genModelListOpen;
    renderGenDialog();
  });
}
genLengthSlider.addEventListener('input', () => {
  const model = genSelectedModel('video');
  const raw = Number(genLengthSlider.value);
  const snapped = Array.isArray(model?.length) && model.length.length ? snapToLength(model.length, raw) : raw;
  genState.video.length = snapped;
  genLengthSlider.value = String(snapped);
  genLengthValue.textContent = `${snapped} Sec`;
  for (const tick of genLengthTicks.children) {
    tick.classList.toggle('is-active', Number(tick.dataset.value) === snapped);
  }
});

function buildGeneratePayload(promptText) {
  const mode = genState.mode;
  const state = genState[mode];
  // Nhánh extend: khóa theo video gốc — không references/res/length, qty 1.
  // Khớp validate backend: spec_entry bị skip khi vmode extend (model gốc có
  // thể không còn trong catalog), ratio phải thuộc 16:9/9:16 (UI đã chặn).
  if (mode === 'video' && state.vmode === 'extend') {
    if (!extendState) {
      state.vmode = 'text'; // state mồ côi (hiếm) → về text thay vì payload lỗi
    } else {
      return {
        mode: 'video',
        model: extendState.model,
        ratio: extendState.ratio,
        qty: 1,
        res: null,
        length: null,
        vmode: 'extend',
        extend_item_id: extendState.itemId,
        references: [],
        prompt: promptText,
      };
    }
  }
  const model = genSelectedModel(mode);
  if (!model) return null;
  // Ảnh tham chiếu lấy từ selection trong stage (thứ tự chọn), chỉ item ảnh
  // còn tồn tại; cap theo mode/vmode (image 10, Frames 2, Ingredients 3).
  const selectedImages = selectedImageItems().map((item) => String(item.id));
  const vmode = mode === 'video' ? state.vmode : 'text';
  let references = [];
  if (mode === 'image') {
    references = model?.refs === true ? selectedImages.slice(0, MAX_REFS.image) : [];
  } else if (vmode === 'frames' && model?.frames === true) {
    const slots = getFrameSlots(activeProject);
    references = [slots.startId, slots.endId].filter(Boolean);
  } else if (vmode === 'ingredients' && model?.refs === true) {
    references = selectedImages.slice(0, MAX_REFS.video);
  }
  return {
    mode,
    model: model.id,
    ratio: state.ratio,
    qty: state.qty,
    // Only send res/length the selected model actually supports.
    res: Array.isArray(model.res) && model.res.length ? state.res ?? null : null,
    length: mode === 'video' && Array.isArray(model.length) && model.length.length ? state.length : null,
    vmode,
    references,
    prompt: promptText,
  };
}

let creatingProject = false; // guards a double-send while no project exists yet
let lastSubmitAt = 0; // guard double-fire vi mô (Enter/send liên tiếp <300ms)

async function ensureActiveProject() {
  const existing = projects.find(({ id }) => id === activeProject);
  if (existing) return existing;
  if (creatingProject) return null;
  // Mirror the New project handler so sending always has a target project.
  creatingProject = true;
  try {
    const response = await request('/playground/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name: defaultProjectName(), prompt: prompt.value, results: [] }),
    });
    if (!response.ok) throw new Error('Project creation failed');
    const project = parseProject(await response.json());
    if (!project) throw new Error('Invalid project response');
    projects = [project, ...projects.filter(({ id }) => id !== project.id)];
    sortProjects();
    activeProject = project.id;
    renderProjects();
    showProjectContents();
    return project;
  } catch {
    showToast('Chưa thể tạo project.');
    return null;
  } finally {
    creatingProject = false;
  }
}

async function startGeneration() {
  const promptText = prompt.value.trim();
  if (!promptText) return;
  if (!(genState.mode === 'video' && genState.video.vmode === 'extend') && !genSelectedModel(genState.mode)) {
    showToast('API key hiện tại chưa có model khả dụng cho chế độ này.');
    return;
  }
  if (isFramesMode()) {
    const slots = getFrameSlots(activeProject);
    if (!slots.startId) {
      const startEl = frameSlotEls.start;
      if (startEl) {
        startEl.classList.remove('is-attention');
        void startEl.offsetWidth;
        startEl.classList.add('is-attention');
        setTimeout(() => startEl.classList.remove('is-attention'), 1200);
      }
      showToast(
        slots.endId
          ? 'Cần chọn Start frame (hoặc bấm ⇄ để chuyển End frame sang Start).'
          : 'Cần chọn ít nhất Start frame để tạo video Frames.',
      );
      return;
    }
  }
  // Composer không khoá khi có job chạy — nhưng 1 cú click/Enter không được bắn
  // 2 job: chặn double-fire trong 300ms (không phải khóa composer).
  const now = Date.now();
  if (now - lastSubmitAt < 300) return;
  lastSubmitAt = now;
  const project = await ensureActiveProject();
  if (!project) return;
  const payload = buildGeneratePayload(promptText);
  if (!payload) return;
  const launched = await launchGeneration(project, payload, { clearPrompt: true });
  // Job đã được server nhận → thoát chế độ nối tiếp (chip tắt, composer tự do).
  // Bắn lỗi thì giữ nguyên extend để chỉnh prompt gửi lại.
  if (launched) exitExtendMode();
}

// Đường chung cho generate: dựng pending card tạm → POST → thay bằng job thật
// (jobId server trả) → poll. Mỗi lần gọi là một job độc lập xếp hàng.
async function launchGeneration(project, payload, { clearPrompt = false } = {}) {
  const tmpId = `tmp:${Date.now()}:${tmpJobSeq++}`;
  jobsById.set(tmpId, {
    projectId: project.id,
    jobId: tmpId,
    mode: payload.mode,
    ratio: payload.ratio,
    count: payload.qty,
    startedAt: Date.now(),
    partial: [],
  });
  renderResultsGrid();
  let response;
  try {
    response = await request(projectPath(project.id, '/generate'), {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  } catch {
    removeJob(tmpId);
    renderResultsGrid();
    showToast('Không kết nối được với máy chủ.');
    return false;
  }
  if (response.status === 401 || response.status === 403) {
    // Session expired: back to the key screen.
    removeJob(tmpId);
    setSignedIn(false);
    keyInput.focus();
    return false;
  }
  if (!response.ok) {
    // 429 vượt PLAYGROUND_MAX_RUNNING_JOBS hoặc lỗi khác: job không được tạo,
    // composer vẫn dùng được — hiển thị detail từ response nếu có.
    let message = 'Không tạo được nội dung.';
    try {
      const data = await response.json();
      if (data?.detail) message = String(data.detail);
    } catch { /* Keep the generic message. */ }
    removeJob(tmpId);
    renderResultsGrid();
    showToast(message);
    return false;
  }
  let jobId = null;
  try {
    jobId = (await response.json())?.job_id ?? null;
  } catch {
    jobId = null;
  }
  const entry = jobsById.get(tmpId);
  if (!jobId || !entry) {
    removeJob(tmpId);
    renderResultsGrid();
    showToast('Không tạo được nội dung.');
    return false;
  }
  // Thay khoá tạm bằng jobId thật của server (persist queue từ đây).
  jobsById.delete(tmpId);
  entry.jobId = jobId;
  if (jobsById.has(jobId)) {
    // Reconcile đã adopt job này (race với tab khác) — chỉ hợp nhất metadata.
    Object.assign(jobsById.get(jobId), {
      mode: entry.mode,
      ratio: entry.ratio,
      count: entry.count,
      partial: entry.partial ?? [],
    });
  } else {
    jobsById.set(jobId, entry);
  }
  persistJobQueue();
  if (clearPrompt) {
    // Job đã được server nhận — xoá nội dung đã gửi khỏi ô chat (kèm xoá draft
    // phía server ở nền, không cần chờ).
    clearTimeout(promptSaveTimer);
    prompt.value = '';
    project.prompt = '';
    autoGrowPrompt();
    syncComposerState();
    request(projectPath(project.id), {
      method: 'PATCH',
      body: JSON.stringify({ prompt: '' }),
    }).catch(() => { /* draft sync best-effort */ });
  }
  startPolling(jobId);
  return true;
}

sendButton.addEventListener('click', async () => {
  await startGeneration();
});

prompt.addEventListener('keydown', (event) => {
  // Enter sends; Shift+Enter keeps the newline; ignore IME composition.
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  startGeneration();
});

document.addEventListener('click', (event) => {
  // composedPath keeps the original ancestor chain even when a handler has
  // re-rendered (and thus detached) the clicked node mid-dispatch.
  const path = event.composedPath();
  if (openActions && !path.includes(openActions.toggle.parentElement)) closeProjectActions();
  if (openResultMenu && !path.includes(openResultMenu.actions) && !path.includes(openResultMenu.toggle)) {
    closeResultMenu();
  }
  if (!genDialog.hidden && !path.includes(genDialog) && !path.includes(genTrigger)) {
    setGenDialogOpen(false);
  }
  if (framePicker && !framePicker.hidden && !path.includes(framePicker) && !path.some((n) => n instanceof Element && n.classList?.contains('frame-slot'))) {
    closeFramePicker();
  }
});
document.addEventListener('keydown', (event) => {
  // Gallery ảnh đang mở: ←/→ duyệt ảnh (mới hơn / cũ hơn).
  if (!lightbox.hidden && galleryState) {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      galleryShift(-1);
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      galleryShift(1);
      return;
    }
  }
  if (event.key !== 'Escape') return;
  clearDropHighlights();
  if (marqueeState) {
    event.preventDefault();
    endMarquee();
    return;
  }
  if (openResultMenu) {
    event.preventDefault();
    closeResultMenu(true);
    return;
  }
  if (itemDialogState) {
    event.preventDefault();
    closeItemDialog();
    return;
  }
  if (!maskEditor.hidden) {
    event.preventDefault();
    closeMaskEditor();
    return;
  }
  if (!wmModal.hidden) {
    event.preventDefault();
    wmModal.hidden = true;
    return;
  }
  if (!videoEditor.hidden) {
    // Editor đè lên các modal khác — đóng nó trước.
    event.preventDefault();
    closeVideoEditor();
    return;
  }
  if (!lightbox.hidden) {
    // The lightbox sits on top; close it before the config dialog.
    event.preventDefault();
    closeLightbox();
    return;
  }
  if (openActions) {
    event.preventDefault();
    closeProjectActions(true);
  }
  if (framePicker && !framePicker.hidden) {
    event.preventDefault();
    closeFramePicker();
    return;
  }
  if (!genDialog.hidden) {
    event.preventDefault();
    if (genModelListOpen) {
      genModelListOpen = false;
      renderGenDialog();
    } else {
      setGenDialogOpen(false);
      genTrigger.focus();
    }
  }
});

logout.addEventListener('click', async () => {
  try { await request('/playground/logout', { method: 'POST' }); } catch { /* Clear local UI regardless. */ }
  setSignedIn(false);
  focusLoginField();
});
window.addEventListener('pagehide', () => {
  keyInput.value = '';
  clearTimeout(promptSaveTimer);
  if (!workspace.hidden) setSignedIn(false);
});
window.addEventListener('pageshow', (event) => {
  if (event.persisted) restoreSession();
});
buildStaticRows();
renderGenDialog();
renderResultsGrid();
restoreSession();

// --- Items: rename / re-use prompt / download / xóa --------------------------

const itemDialog = document.querySelector('#item-dialog');
const itemDialogForm = document.querySelector('#item-dialog-form');
const itemDialogTitle = document.querySelector('#item-dialog-title');
const itemNameInput = document.querySelector('#item-name');
const itemDialogError = document.querySelector('#item-dialog-error');
const itemDialogCancel = document.querySelector('#item-dialog-cancel');
const itemDialogSubmit = document.querySelector('#item-dialog-submit');

let openResultMenu = null; // { toggle, actions, projectId, itemId }
let itemDialogState = null; // { mode: 'rename'|'delete', projectId, item }

async function responseErrorMessage(response) {
  try {
    const data = await response.json();
    return typeof data?.detail === 'string' && data.detail ? data.detail : '';
  } catch {
    return '';
  }
}

function activeProjectData() {
  return projects.find(({ id }) => id === activeProject) ?? null;
}

function projectReferenceItems(project) {
  return (Array.isArray(project?.results) ? project.results : [])
    .filter((item) => item && item.kind === 'image' && item.role === 'reference');
}

function replaceProjectWith(updated) {
  const parsed = parseProject(updated);
  if (!parsed) return null;
  projects = [parsed, ...projects.filter(({ id }) => id !== parsed.id)];
  sortProjects();
  // Không focus sidebar ở đây (giật focus khi user đang thao tác chuột).
  renderProjects();
  if (parsed.id === activeProject) {
    // Prune selection: id không còn trong project bị loại.
    const liveIds = new Set((parsed.results ?? []).map((item) => String(item?.id)));
    selectionByProject.set(
      parsed.id,
      selectionIds(parsed.id).filter((id) => liveIds.has(id)),
    );
    renderResultsGrid();
    renderGenDialog();
    renderComposerRefs();
    syncSelectionToolbar();
  }
  return parsed;
}

async function updateProjectItem(projectId, itemId, patch) {
  try {
    const response = await request(projectPath(projectId, `/items/${encodeURIComponent(itemId)}`), {
      method: 'PATCH',
      body: JSON.stringify(patch),
    });
    if (response.status === 401) {
      setSignedIn(false);
      return null;
    }
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không cập nhật được item.');
      return null;
    }
    return replaceProjectWith(await response.json());
  } catch {
    showToast('Không cập nhật được item.');
    return null;
  }
}

async function deleteProjectItem(projectId, itemId) {
  try {
    const response = await request(projectPath(projectId, `/items/${encodeURIComponent(itemId)}`), {
      method: 'DELETE',
    });
    if (response.status === 401) {
      setSignedIn(false);
      return null;
    }
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không xóa được item.');
      return null;
    }
    // DELETE trả 204 rỗng — nạp lại danh sách project cho khớp nguồn sự thật.
    const list = await request('/playground/api/projects');
    if (list.ok) {
      const data = await list.json();
      const updated = (Array.isArray(data) ? data : []).find((p) => p?.id === projectId);
      return replaceProjectWith(updated);
    }
    return null;
  } catch {
    showToast('Không xóa được item.');
    return null;
  }
}

async function createReferenceItem(projectId, { name, image }, { silent = false } = {}) {
  try {
    const response = await request(projectPath(projectId, '/items'), {
      method: 'POST',
      body: JSON.stringify({ name, image }),
    });
    if (response.status === 401) {
      setSignedIn(false);
      return null;
    }
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không tạo được ảnh tham chiếu.');
      return null;
    }
    const project = replaceProjectWith(await response.json());
    if (!silent) showToast('Đã thêm ảnh tham chiếu.');
    return project;
  } catch {
    showToast('Không tạo được ảnh tham chiếu.');
    return null;
  }
}

// ── Menu trên card ───────────────────────────────────────────────────────────

function closeResultMenu(restoreFocus = false) {
  if (!openResultMenu) return;
  const { toggle, actions } = openResultMenu;
  actions.remove();
  toggle.setAttribute('aria-expanded', 'false');
  openResultMenu = null;
  if (restoreFocus) toggle.focus();
}

function positionMenu(actions, rect) {
  const width = actions.offsetWidth;
  const height = actions.offsetHeight;
  let left = rect.right - width;
  let top = rect.bottom + 6;
  if (left < 8) left = 8;
  if (left + width > window.innerWidth - 8) left = window.innerWidth - 8 - width;
  if (top + height > window.innerHeight - 8) top = rect.top - height - 6;
  if (top < 8) top = 8;
  actions.style.left = `${left}px`;
  actions.style.top = `${top}px`;
}

// Tên file an toàn: thay ký tự cấm trong filename bằng '_' (Windows/macOS/Linux).
function sanitizeFilename(name) {
  return String(name ?? '')
    .replace(/[\\/:*?"<>|]+/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120) || 'item';
}

// Download item: fetch blob cùng origin → objectURL → <a download> click.
// Lỗi thì toast, không crash; tên file = item.name + ext theo url/kind.
async function downloadItem(item) {
  const isVideo = item.kind === 'video';
  const url = typeof item.url === 'string' ? item.url : '';
  if (!url) {
    showToast('Item này không có tệp để tải.');
    return;
  }
  const ext = (() => {
    if (isVideo) return '.mp4';
    const match = /\.([A-Za-z0-9]{2,5})(?:[?#]|$)/.exec(url);
    const found = match?.[1]?.toLowerCase();
    if (found === 'jpg' || found === 'jpeg') return '.jpg';
    if (found === 'png' || found === 'webp') return `.${found}`;
    return '.png';
  })();
  try {
    const response = await fetch(url, { credentials: 'same-origin' });
    if (response.status === 401) {
      setSignedIn(false);
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = `${sanitizeFilename(itemDisplayName(item, isVideo))}${ext}`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
  } catch {
    showToast('Không tải được tệp của item này.');
  }
}

// Menu trên card: Bật/tắt tham chiếu (với ảnh) / Đổi tên / Re-use prompt / Download / Xóa.
function openResultMenuFor(toggle, item) {
  const projectId = activeProject;
  if (!projectId) return;
  const actions = document.createElement('div');
  actions.className = 'result-menu-list';
  const refSelected = item.kind === 'image' && isSelected(item.id);
  const refToggleBtn = item.kind === 'image'
    ? makeButton('result-menu-item', refSelected ? 'Bỏ tham chiếu' : 'Dùng làm tham chiếu')
    : null;
  const renameBtn = makeButton('result-menu-item', 'Đổi tên');
  const promptText = typeof item.meta?.prompt === 'string' ? item.meta.prompt.trim() : '';
  const reuseBtn = promptText ? makeButton('result-menu-item', 'Re-use prompt') : null;
  const downloadBtn = makeButton('result-menu-item', 'Download');
  const deleteBtn = makeButton('result-menu-item is-danger', 'Xóa');
  actions.append(
    ...(refToggleBtn ? [refToggleBtn] : []),
    renameBtn,
    ...(reuseBtn ? [reuseBtn] : []),
    downloadBtn,
    deleteBtn,
  );
  document.body.append(actions);
  positionMenu(actions, toggle.getBoundingClientRect());

  if (refToggleBtn) {
    refToggleBtn.addEventListener('click', () => {
      closeResultMenu();
      setSelected(item.id, !refSelected);
    });
  }
  renameBtn.addEventListener('click', () => {
    closeResultMenu();
    openItemDialog('rename', projectId, item);
  });
  if (reuseBtn) {
    reuseBtn.addEventListener('click', () => {
      closeResultMenu();
      reuseItemPrompt(item);
    });
  }
  downloadBtn.addEventListener('click', () => {
    closeResultMenu();
    downloadItem(item);
  });
  deleteBtn.addEventListener('click', () => {
    closeResultMenu();
    openItemDialog('delete', projectId, item);
  });

  openResultMenu = { toggle, actions, projectId, itemId: String(item.id) };
  toggle.setAttribute('aria-expanded', 'true');
}

// ── Item dialog (rename / confirm delete) ───────────────────────────────────

function openItemDialog(mode, projectId, item) {
  itemDialogState = { mode, projectId, item };
  itemDialogError.hidden = true;
  itemDialogSubmit.classList.toggle('is-danger', mode === 'delete');
  if (mode === 'rename') {
    itemDialogTitle.textContent = 'Đổi tên item';
    itemNameInput.hidden = false;
    itemNameInput.value = item.name || '';
    itemDialogSubmit.textContent = 'Lưu';
  } else {
    itemDialogTitle.textContent = 'Xóa item này khỏi dự án?';
    itemNameInput.hidden = true;
    itemNameInput.value = '';
    itemDialogSubmit.textContent = 'Xóa';
  }
  itemDialog.hidden = false;
  if (!itemNameInput.hidden) itemNameInput.focus();
}

function closeItemDialog() {
  itemDialogState = null;
  itemDialog.hidden = true;
}

itemDialogCancel.addEventListener('click', closeItemDialog);
itemDialogForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!itemDialogState) return;
  const { mode, projectId, item } = itemDialogState;
  if (mode === 'rename') {
    const name = itemNameInput.value.trim().slice(0, 200);
    if (!name) {
      itemDialogError.textContent = 'Tên không được để trống.';
      itemDialogError.hidden = false;
      return;
    }
    if (name === (item.name || '')) {
      closeItemDialog();
      return;
    }
    const updated = await updateProjectItem(projectId, String(item.id), { name });
    if (updated) showToast('Đã đổi tên item.');
  } else {
    const removedId = String(item.id);
    setSelected(removedId, false); // loại khỏi selection (nếu có) trước khi xoá
    await deleteProjectItem(projectId, removedId);
  }
  closeItemDialog();
});

// ── Mask WM editor (tô vùng watermark trên ảnh kết quả) ─────────────────────

const maskEditor = document.querySelector('#mask-editor');
const maskCanvas = document.querySelector('#mask-canvas');
const maskBrush = document.querySelector('#mask-brush');
const maskReset = document.querySelector('#mask-reset');
const maskCancel = document.querySelector('#mask-cancel');
const maskSave = document.querySelector('#mask-save');
const maskTitle = document.querySelector('#mask-editor-title');
const maskUnderlay = document.querySelector('#mask-underlay');
const maskNavPrev = document.querySelector('#mask-prev');
const maskNavNext = document.querySelector('#mask-next');
const maskNavLabel = document.querySelector('#mask-nav-label');
const maskSampleGen = document.querySelector('#mask-sample-gen');
const maskEmptyHint = document.querySelector('#mask-empty-hint');

let maskState = null; // { key, ratio, res, base, modelId, strokes, baseMaskCanvas } — canvas trong suốt + nét trắng
let maskNav = null; // { images: [item], index } — ảnh ví dụ + tham khảo duyệt bằng ◀ ▶
let maskEditorReturnToPanel = false; // mở editor từ Mask WM panel → đóng xong quay lại panel
let maskEditorEpoch = 0; // bỏ kết quả GET sample/mask của editor đã đóng/thay thế

// Mask đã lưu (PNG nền đen + nét trắng) → canvas trắng-trên-trong suốt cùng
// kích thước editor, ghép được cả vào màn hình vẽ lẫn bitmap export. Vùng đen
// của file mask phải biến mất (không che ảnh nền) nên ép alpha theo độ sáng 0/255.
function maskWhiteLayerFromImage(img, width, height) {
  const off = document.createElement('canvas');
  off.width = width;
  off.height = height;
  const octx = off.getContext('2d');
  octx.drawImage(img, 0, 0, width, height);
  const frame = octx.getImageData(0, 0, width, height);
  const px = frame.data;
  for (let i = 0; i < px.length; i += 4) {
    // Đọc độ sáng TRƯỚC khi ghi đè kênh màu — mask 0/255, chỉ vùng sáng > 127
    // giữ alpha 255 (nét trắng); nếu ghi 255 vào kênh đỏ trước thì mọi pixel
    // đều "sáng" → cả canvas trắng đục che mất ảnh nền.
    const on = px[i] > 127;
    px[i] = 255;
    px[i + 1] = 255;
    px[i + 2] = 255;
    px[i + 3] = on ? 255 : 0;
  }
  octx.putImageData(frame, 0, 0);
  return off;
}

function maskRedraw() {
  if (!maskState) return;
  const { strokes, baseMaskCanvas } = maskState;
  const ctx = maskCanvas.getContext('2d');
  // Canvas chỉ vẽ mask đã lưu (nếu có) + nét trắng trên nền trong suốt — nền
  // đen do CSS box đảm nhiệm, ảnh tham khảo là element underlay nằm dưới canvas
  // nên không bao giờ lẫn vào nét vẽ; mask khi export (maskSave) vẫn vẽ lại
  // nền đen + mask cũ + nét trắng nguyên bản.
  ctx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
  if (baseMaskCanvas) ctx.drawImage(baseMaskCanvas, 0, 0, maskCanvas.width, maskCanvas.height);
  const brush = Number(maskBrush.value);
  ctx.strokeStyle = '#fff';
  ctx.fillStyle = '#fff';
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const stroke of strokes) {
    if (stroke.length === 1) {
      ctx.beginPath();
      ctx.arc(stroke[0].x, stroke[0].y, brush / 2, 0, Math.PI * 2);
      ctx.fill();
      continue;
    }
    ctx.lineWidth = brush;
    ctx.beginPath();
    ctx.moveTo(stroke[0].x, stroke[0].y);
    for (const point of stroke.slice(1)) ctx.lineTo(point.x, point.y);
    ctx.stroke();
  }
  maskSave.disabled = strokes.length === 0;
}

function maskCanvasPoint(event) {
  const rect = maskCanvas.getBoundingClientRect();
  const scaleX = maskCanvas.width / rect.width;
  const scaleY = maskCanvas.height / rect.height;
  return {
    x: (event.clientX - rect.left) * scaleX,
    y: (event.clientY - rect.top) * scaleY,
  };
}

// Resolve base alias của 1 model id qua catalog gen-options (nguồn sự thật).
function modelBaseFor(modelId) {
  if (typeof modelId !== 'string') return null;
  for (const mode of ['image', 'video']) {
    const spec = (optionsFor(mode)?.models ?? []).find(
      (model) => model.id === modelId || model.base === modelId,
    );
    if (typeof spec?.base === 'string' && spec.base) return spec.base;
  }
  if (modelId === 'nano-banana-pro') return 'gemini-3.0-pro-image';
  return null;
}

// Ảnh tham khảo cho mask key: item image trong project active cùng base model
// + cùng ratio + cùng res với key đang vẽ (WM layout khác theo res — ảnh lệch
// res làm nền sẽ dẫn sai); sort mới nhất trước (results append theo thời gian).
function maskReferenceImages(base, ratio, res = null) {
  const project = activeProjectData();
  if (!project || !base || !ratio) return [];
  const resLower = res ? String(res).toLowerCase() : null;
  return (Array.isArray(project.results) ? project.results : [])
    .filter((item) => item && item.kind === 'image'
      && typeof item.url === 'string'
      && item.meta?.ratio === ratio
      && (!resLower || String(item.meta?.res ?? '').toLowerCase() === resLower)
      && modelBaseFor(item.meta?.model) === base)
    .reverse();
}

// ◀ ▶ chỉ đổi underlay (ảnh mờ dưới canvas) — strokes giữ nguyên vì mask dùng
// chung cả nhóm cùng base + ratio. Hết danh sách thì disable nút tương ứng.
// Không có nền nào → hiện hint và chặn vẽ (tô mù trên nền đen ra mask vô dụng).
function maskSyncNav() {
  const images = maskNav?.images ?? [];
  const hasImages = images.length > 0;
  maskNavPrev.hidden = !hasImages;
  maskNavNext.hidden = !hasImages;
  maskNavLabel.hidden = !hasImages;
  maskEmptyHint.hidden = hasImages;
  maskCanvas.classList.toggle('is-idle', !hasImages);
  if (!hasImages) {
    maskUnderlay.removeAttribute('src');
    maskUnderlay.hidden = true;
    return;
  }
  maskNav.index = Math.min(Math.max(maskNav.index, 0), images.length - 1);
  const item = images[maskNav.index];
  maskUnderlay.src = item.url;
  maskUnderlay.hidden = false;
  maskNavPrev.disabled = maskNav.index === 0;
  maskNavNext.disabled = maskNav.index === images.length - 1;
  maskNavLabel.textContent = `${maskNav.index + 1}/${images.length}`;
}

function maskNavShift(delta) {
  if (!maskNav || !maskNav.images.length) return;
  maskNav.index += delta;
  maskSyncNav();
}

maskNavPrev.addEventListener('click', () => maskNavShift(-1));
maskNavNext.addEventListener('click', () => maskNavShift(1));

// "Gen 1 ảnh ví dụ": tạo 1 ảnh mẫu cho đúng model + tỉ lệ + res của key đang
// vẽ, giữ nguyên watermark làm nền tô mask. Server lưu ảnh vào thư mục mask
// (prefix sample_) → các lần mở editor sau tự có nền sẵn.
maskSampleGen.addEventListener('click', async () => {
  if (!maskState?.modelId || maskSampleGen.disabled) return;
  const requesting = maskState; // editor có thể đóng giữa chừng — so tham chiếu
  maskSampleGen.disabled = true;
  const label = maskSampleGen.textContent;
  maskSampleGen.textContent = 'Đang tạo…';
  try {
    const response = await request('/playground/api/watermark/sample', {
      method: 'POST',
      body: JSON.stringify({ model: requesting.modelId, ratio: requesting.ratio, res: requesting.res }),
    });
    if (maskState !== requesting) return; // đã đóng / mở key khác — bỏ qua
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không tạo được ảnh ví dụ.');
      return;
    }
    const data = await response.json();
    if (!maskNav) maskNav = { images: [], index: 0 };
    maskNav.images = maskNav.images.filter((img) => !img.sample);
    maskNav.images.unshift({ url: data.url, sample: true });
    maskNav.index = 0;
    maskSyncNav();
    showToast('Đã tạo ảnh ví dụ — lưu lại làm nền vẽ mask cho lần sau.');
  } catch {
    if (maskState === requesting) showToast('Không tạo được ảnh ví dụ.');
  } finally {
    maskSampleGen.disabled = false;
    maskSampleGen.textContent = label;
  }
});

// Mở mask editor cho 1 key bất kỳ từ panel — canvas theo tỉ lệ của key. Nền vẽ
// là ảnh ví dụ đã lưu cho key (nếu có, luôn đứng đầu) + ảnh cùng base model +
// ratio + res trong project active (◀ ▶ duyệt, label "i/n" dưới cạnh). Chưa có
// nền nào thì chặn vẽ — phải bấm "Gen 1 ảnh ví dụ" trước.
function openMaskEditorForKey(key, ratio, base = null, modelId = null, res = null) {
  const shape = (() => {
    if (typeof ratio !== 'string' || !/^\d+:\d+$/.test(ratio)) return { w: 1, h: 1 };
    const [w, h] = ratio.split(':').map(Number);
    return w && h ? { w, h } : { w: 1, h: 1 };
  })();
  const longSide = 1024;
  const horizontal = shape.w >= shape.h;
  const width = horizontal ? longSide : Math.max(1, Math.round((longSide * shape.w) / shape.h));
  const height = horizontal ? Math.max(1, Math.round((longSide * shape.h) / shape.w)) : longSide;
  // Editor đè lên panel (z-index thấp hơn) → đóng panel tạm, lưu ý quay lại sau.
  maskEditorReturnToPanel = true;
  wmModal.hidden = true;
  const state = { key, ratio, res, base, modelId, strokes: [], baseMaskCanvas: null };
  maskState = state;
  maskCanvas.width = width;
  maskCanvas.height = height;
  maskTitle.textContent = `Vẽ vùng WM cần xoá — ${key}`;
  const images = base ? maskReferenceImages(base, ratio, res) : [];
  maskNav = { images, index: 0 };
  const epoch = ++maskEditorEpoch;
  maskSyncNav();
  maskEditor.hidden = false;
  maskRedraw();
  // Ảnh ví dụ đã lưu cho key (GET trả 404 nếu chưa có) nạp không đồng bộ —
  // có thì đẩy lên đầu danh sách underlay.
  const sampleUrl = `/playground/api/watermark/sample/${encodeURIComponent(sanitizeMaskKey(key))}`;
  request(sampleUrl).then(async (response) => {
    if (epoch !== maskEditorEpoch || !response.ok || maskState !== state || !maskNav) return;
    if (maskNav.images.some((img) => img.sample)) return;
    maskNav.images.unshift({ url: sampleUrl, sample: true });
    maskNav.index = 0;
    maskSyncNav();
  }).catch(() => { /* chưa có sample — dùng ảnh tham khảo trong project */ });
  // Mask đã lưu cho key nạp không đồng bộ về hiện trên canvas: vẽ thêm là
  // NỐI vào mask cũ (export gộp cả hai), "Đặt lại" mới xoá cả mask cũ.
  const maskUrl = `/playground/api/watermark/mask/${encodeURIComponent(sanitizeMaskKey(key))}`;
  request(maskUrl).then(async (response) => {
    if (epoch !== maskEditorEpoch || !response.ok || maskState !== state) return;
    const blobUrl = URL.createObjectURL(await response.blob());
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(blobUrl);
      if (epoch !== maskEditorEpoch || maskState !== state) return;
      state.baseMaskCanvas = maskWhiteLayerFromImage(img, width, height);
      maskRedraw();
    };
    img.onerror = () => URL.revokeObjectURL(blobUrl);
    img.src = blobUrl;
  }).catch(() => { /* chưa có mask cho key */ });
}

function closeMaskEditor() {
  const backToPanel = maskEditorReturnToPanel;
  maskEditorReturnToPanel = false;
  maskState = null;
  maskNav = null;
  maskEditorEpoch += 1; // response sample cũ không được chạm vào editor mới
  maskSyncNav(); // ẩn underlay + nút duyệt
  maskEditor.hidden = true;
  if (backToPanel && wmModal.hidden) {
    // Mở từ panel (vẽ/xóa/lưu xong) → quay lại panel và nạp trạng thái mới.
    wmModal.hidden = false;
    loadWmInfo();
  }
}

maskCanvas.addEventListener('pointerdown', (event) => {
  if (!maskState || !maskNav?.images.length) return; // chưa có nền → không cho tô mù
  event.preventDefault();
  maskCanvas.setPointerCapture(event.pointerId);
  maskState.strokes.push([maskCanvasPoint(event)]);
  maskRedraw();
});
maskCanvas.addEventListener('pointermove', (event) => {
  if (!maskState || !maskState.strokes.length) return;
  if (!(event.buttons & 1)) return;
  maskState.strokes[maskState.strokes.length - 1].push(maskCanvasPoint(event));
  maskRedraw();
});
maskCanvas.addEventListener('pointerup', () => maskRedraw());
maskBrush.addEventListener('input', maskRedraw);
maskReset.addEventListener('click', () => {
  if (!maskState) return;
  // Vẽ lại từ đầu: xoá cả nét mới lẫn mask cũ đã nạp — nếu chỉ muốn bỏ nét
  // mới thì Hủy (không Lưu) là đủ.
  maskState.strokes = [];
  maskState.baseMaskCanvas = null;
  maskRedraw();
});
maskCancel.addEventListener('click', closeMaskEditor);
maskSave.addEventListener('click', async () => {
  if (!maskState || !maskState.strokes.length) return;
  const { key, strokes, baseMaskCanvas } = maskState;
  // Mask export: nền đen + mask cũ (nếu có — vẽ thêm là nối vào mask cũ) +
  // nét trắng đục cùng brush — server chuẩn hoá 0/255. Kích thước lấy theo
  // canvas đang vẽ (khớp bitmap khi mở từ item; theo tỉ lệ của key khi mở từ
  // panel không có ảnh gốc).
  const out = document.createElement('canvas');
  out.width = maskCanvas.width;
  out.height = maskCanvas.height;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, out.width, out.height);
  if (baseMaskCanvas) ctx.drawImage(baseMaskCanvas, 0, 0, out.width, out.height);
  const brush = Number(maskBrush.value);
  ctx.strokeStyle = '#fff';
  ctx.fillStyle = '#fff';
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  for (const stroke of strokes) {
    if (stroke.length === 1) {
      ctx.beginPath();
      ctx.arc(stroke[0].x, stroke[0].y, brush / 2, 0, Math.PI * 2);
      ctx.fill();
      continue;
    }
    ctx.lineWidth = brush;
    ctx.beginPath();
    ctx.moveTo(stroke[0].x, stroke[0].y);
    for (const point of stroke.slice(1)) ctx.lineTo(point.x, point.y);
    ctx.stroke();
  }
  const dataUrl = out.toDataURL('image/png');
  const savedKey = key;
  closeMaskEditor();
  try {
    const response = await request(
      `/playground/api/watermark/mask/${encodeURIComponent(savedKey)}`,
      { method: 'POST', body: JSON.stringify({ image: dataUrl }) },
    );
    if (!response.ok) {
      showToast((await responseErrorMessage(response)) || 'Không lưu được mask.');
      return;
    }
    showToast(`Đã lưu mask ${savedKey} — lần tạo sau sẽ tự xoá WM.`);
    // Panel đang mở (quay lại từ editor) → làm mới trạng thái mask.
    if (!wmModal.hidden) loadWmInfo();
  } catch {
    showToast('Không lưu được mask.');
  }
});

// ── Mask WM panel (quản lý mask theo model image × ratio/res) ───────────────

const wmModal = document.querySelector('#wm-modal');
const wmEngine = document.querySelector('#wm-engine');
const wmMaskGrid = document.querySelector('#wm-mask-grid');
const wmOther = document.querySelector('#wm-other');
const wmOtherCount = document.querySelector('#wm-other-count');
const wmOtherList = document.querySelector('#wm-other-list');
const wmFilterActive = document.querySelector('#wm-filter-active');
const wmClose = document.querySelector('#wm-close');

let wmInfo = null; // { enabled, engine, masks } từ GET /playground/api/watermark

// Mirror hàm _mask_keys_for phía backend: key chính "{base}:{ratio}:{res}"
// (WM layout khác nhau theo res — 1K ≠ 2K nên mask tách theo res) + fallback
// "{base}-{orient}-{res}" rồi "{base}-{orient}". Orientation mirror đủ 5 nhóm
// của ASPECT_RATIO_MAP (catalog.py) — backend sinh fallback key theo nhóm này,
// thu gọn nhị phân portrait/landscape sẽ khớp sai cho 4:3 / 1:1 / 3:4.
const MASK_RATIO_ORIENTATIONS = {
  '16:9': 'landscape',
  '4:3': 'four-three',
  '1:1': 'square',
  '3:4': 'three-four',
  '9:16': 'portrait',
};

function maskOrientation(ratio) {
  return MASK_RATIO_ORIENTATIONS[ratio] ?? (ratio === '9:16' ? 'portrait' : 'landscape');
}

// Keys của 1 combo model + ratio + res (image sau validate luôn có res, mặc
// định 1K — mirror backend; không res thì rơi về key cũ theo ratio).
function maskKeysFor(base, ratio, res = null) {
  if (!base || !ratio) return [];
  const orient = maskOrientation(ratio);
  const resLower = res ? String(res).toLowerCase() : null;
  if (!resLower) return [`${base}:${ratio}`, `${base}-${orient}`];
  return [`${base}:${ratio}:${resLower}`, `${base}-${orient}-${resLower}`, `${base}-${orient}`];
}

// Mirror sanitize_key (app/watermark.py): server lưu file mask + trả về key
// dạng filename stem ĐÃ sanitize ("gemini-3.0-pro-image:9:16" →
// "gemini-3.0-pro-image_9_16"). So khớp phải sanitize trước, nếu không panel
// vĩnh viễn báo "Chưa có mask" cho key có ký tự ngoài [A-Za-z0-9._-] (dấu ':').
function sanitizeMaskKey(key) {
  const safe = String(key ?? '').replace(/[^A-Za-z0-9._-]/g, '_').replace(/^[._]+|[._]+$/g, '');
  return safe || 'default';
}

function savedMaskKeys() {
  return new Set((Array.isArray(wmInfo?.masks) ? wmInfo.masks : []).map((mask) => String(mask.key)));
}

function hasSavedMask(key) {
  return savedMaskKeys().has(sanitizeMaskKey(key));
}

// Model image có base alias (điều kiện compose mask key); gen-options là nguồn
// sự thật — model tĩnh offline không có base nên bị lọc ra.
function wmImageModels() {
  const models = Array.isArray(optionsFor('image')?.models) ? optionsFor('image').models : [];
  return models.filter((model) => typeof model.base === 'string' && model.base);
}

function wmModelRatios(model) {
  return Array.isArray(model.ratios) && model.ratios.length ? model.ratios : GEN_OPTIONS.image.ratios;
}

function wmActiveModel() {
  const models = wmImageModels();
  return models.find((model) => model.id === genState.image.model) ?? models[0] ?? null;
}

async function openWmPanel() {
  wmModal.hidden = false;
  // Panel cần base alias từ gen-options; chưa nạp thì nạp trước (best-effort).
  if (!GEN_CAPS) await loadGenOptions();
  await loadWmInfo();
}

async function loadWmInfo() {
  try {
    const response = await request('/playground/api/watermark');
    if (!response.ok) throw new Error('load failed');
    wmInfo = await response.json();
    renderWmPanel();
  } catch {
    wmEngine.textContent = 'Không tải được trạng thái watermark.';
  }
}

async function wmDeleteKeys(keys) {
  let failure = false;
  for (const target of keys) {
    try {
      const response = await request(
        `/playground/api/watermark/mask/${encodeURIComponent(target)}`,
        { method: 'DELETE' },
      );
      if (!response.ok && response.status !== 404) failure = true;
    } catch {
      failure = true;
    }
  }
  if (failure) showToast('Không xóa được một số mask.');
  await loadWmInfo();
}

function wmMaskRow(model, ratio, res) {
  const keys = maskKeysFor(model.base, ratio, res);
  const existing = keys.filter((key) => hasSavedMask(key));
  const row = document.createElement('div');
  row.className = `wm-mask-item${existing.length ? ' has-mask' : ''}`;
  row.setAttribute('role', 'listitem');
  const status = document.createElement('span');
  status.className = `wm-mask-status${existing.length ? ' is-ok' : ''}`;
  status.textContent = existing.length ? '✓' : '';
  status.setAttribute('aria-label', existing.length ? 'Đã có mask' : 'Chưa có mask');
  status.title = existing.length ? `Đã có mask: ${existing.join(', ')}` : 'Chưa có mask cho key này';
  const info = document.createElement('div');
  info.className = 'wm-mask-info';
  const name = document.createElement('span');
  name.className = 'wm-mask-name';
  name.textContent = `${model.label} · ${ratio}${res ? ` · ${res}` : ''}`;
  const key = document.createElement('span');
  key.className = 'wm-mask-key';
  key.textContent = keys[0];
  info.append(name, key);
  const tools = document.createElement('div');
  tools.className = 'wm-mask-tools';
  const draw = makeButton('wm-mask-draw', 'Vẽ mask');
  draw.title = existing.length ? 'Vẽ lại mask (ghi đè)' : 'Vẽ mask cho key này';
  draw.addEventListener('click', () => openMaskEditorForKey(keys[0], ratio, model.base, model.id, res));
  tools.append(draw);
  if (existing.length) {
    const remove = makeButton('wm-mask-delete', 'Xóa');
    remove.addEventListener('click', () => wmDeleteKeys(existing));
    tools.append(remove);
  }
  row.append(status, info, tools);
  return row;
}

function renderWmPanel() {
  if (!wmInfo) return;
  const engine = typeof wmInfo.engine === 'string' ? wmInfo.engine : 'disabled';
  wmEngine.textContent = wmInfo.enabled
    ? `Đang bật · engine: ${engine} · ${Array.isArray(wmInfo.masks) ? wmInfo.masks.length : 0} mask`
    : 'Đang tắt (WATERMARK_ENABLED=0) — mask vẫn lưu được nhưng sẽ không được áp dụng';

  const onlyActive = wmFilterActive.checked;
  const activeModel = wmActiveModel();
  const rows = [];
  const knownKeys = new Set();
  for (const model of wmImageModels()) {
    const ratios = wmModelRatios(model);
    if (onlyActive && activeModel && model.id !== activeModel.id) continue;
    // WM layout khác nhau theo res (1K ≠ 2K) → mỗi res một dòng mask riêng.
    const resList = Array.isArray(model.res) && model.res.length ? model.res : [null];
    for (const ratio of ratios) {
      for (const res of resList) rows.push(wmMaskRow(model, ratio, res));
      for (const res of resList) {
        for (const key of maskKeysFor(model.base, ratio, res)) {
          // mask.key từ server đã sanitize — knownKeys phải cùng dạng mới lọc đúng
          // danh sách "mask khác (legacy)", không dính mask của key hợp lệ.
          knownKeys.add(sanitizeMaskKey(key));
        }
      }
    }
  }
  wmMaskGrid.replaceChildren(...rows);
  wmMaskGrid.hidden = rows.length === 0;

  // Mask không khớp model/ratio nào (legacy, __global__…) — giữ để còn dọn dẹp.
  const others = (Array.isArray(wmInfo.masks) ? wmInfo.masks : [])
    .filter((mask) => !knownKeys.has(String(mask.key)));
  wmOther.hidden = others.length === 0;
  wmOtherCount.textContent = String(others.length);
  wmOtherList.replaceChildren(...others.map((mask) => {
    const li = document.createElement('li');
    li.className = 'wm-mask-row';
    const name = document.createElement('span');
    name.className = 'wm-mask-key';
    name.textContent = mask.key === '__global__' ? 'watermark.png (dùng chung)' : mask.key;
    const remove = makeButton('wm-mask-delete', 'Xóa');
    remove.addEventListener('click', () => wmDeleteKeys([String(mask.key)]));
    li.append(name, remove);
    return li;
  }));
}

wmSettingsButton.addEventListener('click', () => {
  openWmPanel();
});
wmClose.addEventListener('click', () => { wmModal.hidden = true; });
wmModal.addEventListener('click', (event) => {
  if (event.composedPath()[0] === wmModal) wmModal.hidden = true;
});
wmFilterActive.addEventListener('change', renderWmPanel);
