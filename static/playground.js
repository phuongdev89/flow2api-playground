const login = document.querySelector('#login');
const form = document.querySelector('#login-form');
const keyInput = document.querySelector('#api-key');
const loginError = document.querySelector('#login-error');
const loginButton = document.querySelector('#login-button');
const workspace = document.querySelector('#workspace');
const logout = document.querySelector('#logout');
const wmSettingsButton = document.querySelector('#wm-settings');
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
const composerRefs = document.querySelector('#composer-refs');
const resultsGrid = document.querySelector('#results-grid');
const resultsEmpty = document.querySelector('#results-empty');

let projects = [];
let activeProject = null;
let openActions = null;
let resultColumns = 10;
let promptSaveTimer;
let toastTimer;
// Generation jobs live server-side; these maps only track what this session knows.
const activeJobs = new Map(); // project id -> job id
const jobTimers = new Map(); // project id -> poll interval handle
const submitting = new Set(); // project ids with a generate POST in flight
const pendingByProject = new Map(); // project id -> { mode, ratio, count, startedAt }
const jobResults = new Map(); // project id -> results delivered so far for the running job
const COLUMNS_STORAGE_KEY = 'playground-columns';

// ── Selection (bôi chọn tham chiếu trong stage) ─────────────────────────
// Chọn = tham chiếu: thứ tự chọn quyết định chip trong composer; role
// 'reference' trên server được sync ngầm để giữ selection qua reload.
const MAX_REFS = { image: 10, video: 3 };
const selectionByProject = new Map(); // project id -> [item id theo thứ tự chọn]
const roleSyncTimers = new Map(); // project id -> debounce handle

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
      clearJobState(project.id);
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

function jobBusy(projectId) {
  return Boolean(projectId && (activeJobs.has(projectId) || submitting.has(projectId)));
}

function syncComposerState() {
  const busy = jobBusy(activeProject);
  genTrigger.disabled = busy;
  sendButton.disabled = busy || !prompt.value.trim();
}

function showProjectContents() {
  const active = projects.find(({ id }) => id === activeProject);
  prompt.value = active?.prompt ?? '';
  autoGrowPrompt();
  syncComposerState();
  resumePollingIfNeeded(activeProject);
  // Selection khôi phục từ role đã sync server (reload giữ nguyên lựa chọn).
  selectionByProject.set(
    activeProject,
    projectReferenceItems(active).map((item) => String(item.id)),
  );
  renderResultsGrid();
  renderComposerRefs();
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

function selectionChanged() {
  applyVideoVmodeAuto();
  renderResultsGrid();
  renderComposerRefs();
  syncSelectionRoles();
}

// Auto-chọn vmode theo số ảnh đang chọn (giữ combo luôn hợp lệ):
// 0 ảnh → Text; 1-2 ảnh → Frames (nếu model hỗ trợ); ≥3 → Ingredients.
function applyVideoVmodeAuto() {
  if (genState.mode !== 'video' || genState.video.vmode === 'extend') return;
  const model = genSelectedModel('video');
  const count = selectedImageItems().length;
  if (count === 0) {
    genState.video.vmode = 'text';
    return;
  }
  if (genState.video.vmode === 'text' && count > 0) {
    genState.video.vmode = count <= 2 && model?.frames === true ? 'frames'
      : model?.refs === true ? 'ingredients' : 'text';
    return;
  }
  if (genState.video.vmode === 'frames' && count > 2) {
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
  if (!composerRefs) return;
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
    if (!unusable && !overflow) {
      cross.addEventListener('click', (event) => {
        event.stopPropagation();
        setSelected(item.id, false);
      });
    } else {
      // Chip vô dụng/vượt hạn: bấm để loại khỏi selection cho gọn.
      cross.addEventListener('click', (event) => {
        event.stopPropagation();
        setSelected(item.id, false);
      });
    }
    chip.append(img, cross);
    return chip;
  });
  const counter = document.createElement('span');
  counter.className = 'composer-refs-count';
  const usable = Math.min(imageCount, cap);
  const videoLabels = {
    text: `Text mode — ${imageCount} ảnh không dùng`,
    frames: `Frames ${usable}/2`,
    ingredients: `Ingredients ${usable}/3`,
  };
  counter.textContent = genState.mode === 'video'
    ? videoLabels[genState.video.vmode] ?? videoLabels.text
    : `Ảnh tham chiếu ${usable}/${cap}`;
  composerRefs.replaceChildren(counter, ...chips);
  composerRefs.hidden = false;
}

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
    // Inline video keeps its controls; this corner button opens the lightbox.
    const expand = makeButton('result-expand', '');
    expand.setAttribute('aria-label', 'Xem lớn');
    expand.title = 'Xem lớn';
    expand.append(expandIcon());
    card.append(expand);
  }
  return card;
}

function createPendingCard(pending) {
  const card = document.createElement('div');
  card.className = 'result-card is-pending';
  card.setAttribute('role', 'status');
  card.setAttribute('aria-busy', 'true');
  card.setAttribute('aria-label', pending.mode === 'video' ? 'Đang tạo video…' : 'Đang tạo ảnh…');
  card.style.aspectRatio = aspectFromRatio(pending.ratio) ?? (pending.mode === 'video' ? '16 / 9' : '1 / 1');
  const spinner = document.createElement('span');
  spinner.className = 'result-spinner';
  card.append(spinner);
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
  const pending = pendingByProject.get(active.id);
  const cards = [];
  if (pending) {
    // Progressive results claim the pending slots from the front.
    const partial = (jobResults.get(active.id) ?? []).slice(0, pending.count);
    for (const item of partial) {
      const card = createResultCard(item);
      if (card) cards.push(card);
    }
    while (cards.length < pending.count) cards.push(createPendingCard(pending));
  }
  for (const item of results) {
    const card = createResultCard(item);
    if (card) cards.push(card);
  }
  resultsGrid.replaceChildren(...cards);
  resultsGrid.hidden = cards.length === 0;
  resultsEmpty.hidden = cards.length > 0;
}

// --- Lightbox ---------------------------------------------------------------

const lightbox = document.createElement('div');
lightbox.id = 'lightbox';
lightbox.className = 'lightbox';
lightbox.hidden = true;
document.body.append(lightbox);

function openLightbox(kind, url) {
  if (!url) return;
  const isVideo = kind === 'video';
  const media = document.createElement(isVideo ? 'video' : 'img');
  media.className = isVideo ? 'lightbox-video' : 'lightbox-img';
  if (isVideo) {
    media.controls = true;
    media.preload = 'metadata';
  } else {
    media.alt = '';
  }
  media.src = url;
  const closeButton = makeButton('lightbox-close', '×');
  closeButton.setAttribute('aria-label', 'Đóng');
  closeButton.addEventListener('click', closeLightbox);
  lightbox.replaceChildren(media, closeButton);
  lightbox.hidden = false;
  closeButton.focus();
}

function closeLightbox() {
  if (lightbox.hidden) return;
  lightbox.hidden = true;
  // Dropping the media node stops video playback right away.
  lightbox.replaceChildren();
}

lightbox.addEventListener('click', (event) => {
  // composedPath: same re-render-safe pattern as the dialog click-outside.
  if (event.composedPath()[0] === lightbox) closeLightbox();
});

resultsGrid.addEventListener('click', (event) => {
  const path = event.composedPath();
  const card = path.find(
    (node) => node instanceof Element && node.classList.contains('result-card') && !node.classList.contains('is-pending')
  );
  if (!card) return;
  // Ctrl/Cmd + click = toggle chọn item làm tham chiếu (không mở lightbox).
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
  const onExpand = path.some((node) => node instanceof Element && node.classList.contains('result-expand'));
  // Inline video owns its own clicks; only the expand button opens the lightbox.
  if (media.tagName === 'VIDEO' && !onExpand) return;
  openLightbox(media.tagName === 'VIDEO' ? 'video' : 'image', media.getAttribute('src'));
});

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

function stopPolling(projectId) {
  const timer = jobTimers.get(projectId);
  if (timer) clearInterval(timer);
  jobTimers.delete(projectId);
}

function clearJobState(projectId) {
  submitting.delete(projectId);
  activeJobs.delete(projectId);
  stopPolling(projectId);
  pendingByProject.delete(projectId);
  jobResults.delete(projectId);
}

function startPolling(projectId, jobId) {
  stopPolling(projectId);
  const timer = setInterval(() => { pollJob(projectId, jobId); }, 1500);
  jobTimers.set(projectId, timer);
}

function resumePollingIfNeeded(projectId) {
  // A job keeps running server-side across project switches; resume its poll
  // when its project becomes active again and no interval is tracking it.
  if (!projectId || !activeJobs.has(projectId) || jobTimers.has(projectId)) return;
  startPolling(projectId, activeJobs.get(projectId));
}

// Video jobs can run for minutes; no stop shorter than the contract's 10 minutes.
const JOB_MAX_WAIT_MS = 20 * 60 * 1000;

async function pollJob(projectId, jobId) {
  const pending = pendingByProject.get(projectId);
  if (pending && Date.now() - pending.startedAt > JOB_MAX_WAIT_MS) {
    failJob(projectId, 'Hết thời gian chờ tạo nội dung.');
    return;
  }
  let data = null;
  try {
    const response = await request(projectPath(projectId, `/generate/${encodeURIComponent(jobId)}`));
    if (response.status === 401) {
      // Session hết hạn giữa chừng: poll thêm cũng vô ích — đưa về key screen
      // giống POST path thay vì chờ hết 20 phút rồi báo sai "hết thời gian".
      stopPolling(projectId);
      setSignedIn(false);
      return;
    }
    if (!response.ok) return; // transient failure: keep polling
    data = await response.json();
  } catch {
    return; // network hiccup: keep polling
  }
  if (data?.status === 'done') {
    finishJob(projectId, Array.isArray(data.results) ? data.results : []);
    return;
  }
  if (data?.status === 'error') {
    failJob(projectId, data.error);
    return;
  }
  // Still running: adopt partial results early when the backend streams them.
  if (Array.isArray(data?.results) && data.results.length) {
    jobResults.set(projectId, data.results);
    if (projectId === activeProject) renderResultsGrid();
  }
}

function mergeJobResults(projectId, results) {
  const project = projects.find(({ id }) => id === projectId);
  if (!project || !Array.isArray(results) || !results.length) return;
  const existing = Array.isArray(project.results) ? project.results : [];
  const seen = new Set(existing.map((item) => item?.id).filter((id) => id !== undefined && id !== null));
  project.results = [...existing, ...results.filter((item) => item?.id === undefined || item?.id === null || !seen.has(item.id))];
  project.updated_at = new Date().toISOString();
  sortProjects();
  renderProjects();
}

function finishJob(projectId, results) {
  const pending = pendingByProject.get(projectId);
  const created = Array.isArray(results) ? results.length : 0;
  clearJobState(projectId);
  // The server already appended these results; mirror them into the local copy.
  mergeJobResults(projectId, results);
  if (projectId === activeProject) {
    renderResultsGrid();
  }
  syncComposerState();
  const noun = pending?.mode === 'video' ? 'video' : 'ảnh';
  if (pending && created < pending.count) {
    showToast(`Đã tạo ${created}/${pending.count} ${noun} thành công.`);
  } else {
    showToast(`Đã tạo ${created} ${noun}.`);
  }
}

function failJob(projectId, message) {
  clearJobState(projectId);
  if (projectId === activeProject) renderResultsGrid();
  syncComposerState();
  showToast(message || 'Không tạo được nội dung.');
}

function showError(message = '', invalidKey = false) {
  loginError.hidden = !message;
  loginError.textContent = message;
  if (invalidKey) {
    keyInput.setAttribute('aria-invalid', 'true');
    keyInput.setAttribute('aria-describedby', 'login-error');
  } else {
    keyInput.removeAttribute('aria-invalid');
    keyInput.removeAttribute('aria-describedby');
  }
}

function setSignedIn(signedIn) {
  login.hidden = signedIn;
  workspace.hidden = !signedIn;
  logout.hidden = !signedIn;
  wmSettingsButton.hidden = !signedIn;
  if (signedIn) {
    prompt.focus();
  } else {
    projects = [];
    activeProject = null;
    for (const projectId of [...activeJobs.keys(), ...submitting]) clearJobState(projectId);
    closeProjectActions();
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
    await loadProjects();
    loadGenOptions();
  } catch {
    // Leave the key-entry screen available while offline.
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (loginButton.disabled) return;
  const key = keyInput.value.trim();
  if (!key || /[^\x21-\x7e]/.test(key)) {
    showError('API key không hợp lệ.', true);
    return;
  }
  showError();
  loginButton.disabled = true;
  keyInput.readOnly = true;
  loginButton.textContent = 'Đang xác thực…';
  try {
    const response = await request('/playground/session', {
      method: 'POST',
      headers: { Authorization: `Bearer ${key}` },
    });
    if (response.status === 204) {
      keyInput.value = '';
      setSignedIn(true);
      await loadProjects();
      loadGenOptions();
    } else if (response.status === 401 || response.status === 403) {
      showError('API key không hợp lệ.', true);
    } else {
      showError('Không xác thực được. Vui lòng thử lại.');
    }
  } catch {
    showError('Không kết nối được với máy chủ. Vui lòng thử lại.');
  } finally {
    loginButton.disabled = false;
    keyInput.readOnly = false;
    loginButton.textContent = 'Vào Playground';
    if (!login.hidden) keyInput.focus();
  }
});

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

newProject.addEventListener('click', async () => {
  let number = 1;
  const names = new Set(projects.map(({ name }) => name));
  while (names.has(`Project ${number}`)) number += 1;
  try {
    const response = await request('/playground/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name: `Project ${number}`, prompt: '', results: [] }),
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
  const state = genState[mode];
  if (Array.isArray(model.ratios) && model.ratios.length && !model.ratios.includes(state.ratio)) {
    state.ratio = model.ratios[0];
  }
  if (Array.isArray(model.res) && model.res.length && !model.res.includes(state.res)) {
    state.res = model.res[0];
  }
  if (mode === 'video' && Array.isArray(model.length) && model.length.length) {
    const { values } = lengthSliderSpec(model);
    if (!values.includes(state.length)) state.length = snapToLength(values, state.length);
  }
}

function mergeCapabilities(data) {
  const merged = {};
  for (const mode of ['image', 'video']) {
    const serverModels = Array.isArray(data?.[mode]?.models) ? data[mode].models : [];
    const byId = new Map(
      serverModels
        .filter((model) => model && typeof model.id === 'string')
        .map((model) => [model.id, model])
    );
    const models = optionsFor(mode).models.map((model) => {
      const server = byId.get(model.id);
      if (!server) return { ...model };
      return {
        ...model,
        label: typeof server.label === 'string' && server.label ? server.label : model.label,
        // base alias cần cho mask key WM — server là nguồn sự thật.
        base: typeof server.base === 'string' && server.base ? server.base : model.base,
        ratios: Array.isArray(server.ratios) ? server.ratios : model.ratios,
        res: server.res === undefined ? model.res : server.res,
        length: server.length === undefined ? model.length : server.length,
        refs: typeof server.refs === 'boolean' ? server.refs : model.refs,
        frames: typeof server.frames === 'boolean' ? server.frames : model.frames,
      };
    });
    // Server-only models stay selectable by being appended.
    for (const server of serverModels) {
      if (server && typeof server.id === 'string' && !models.some((model) => model.id === server.id)) {
        models.push({ ...server });
      }
    }
    merged[mode] = { models };
  }
  GEN_CAPS = merged;
  for (const mode of ['image', 'video']) applyModelCapability(mode);
  renderGenDialog();
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
    genRatioRows[mode].replaceChildren(
      ...GEN_OPTIONS[mode].ratios.map((ratio) => {
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
  const model = genSelectedModel(mode);
  const button = genModelButtons[mode];
  button.replaceChildren();
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
      });
      return option;
    })
  );
}

function renderResRow() {
  const resolutions = genSelectedModel('video').res;
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
  const resolutions = genSelectedModel('image').res;
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

function renderLengthRow() {
  const model = genSelectedModel('video');
  if (!Array.isArray(model.length) || !model.length.length) {
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
  // Tick labels under the track: one per supported duration, centred on its snap position.
  genLengthTicks.replaceChildren(
    ...values.map((value) => {
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
    const allowedRatios = genSelectedModel(mode).ratios;
    for (const button of genRatioRows[mode].children) {
      const supported = !Array.isArray(allowedRatios) || allowedRatios.includes(button.dataset.ratio);
      button.classList.toggle('is-disabled', !supported);
      if (!supported) {
        button.setAttribute('aria-disabled', 'true');
        button.title = 'Không hỗ trợ bởi model này';
      } else {
        button.removeAttribute('aria-disabled');
        button.removeAttribute('title');
      }
      const active = button.dataset.ratio === genState[mode].ratio;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    }
    for (const button of genQtyRows[mode].children) {
      const active = Number(button.dataset.qty) === genState[mode].qty;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-pressed', String(active));
    }
    renderModelRow(mode);
    genModelButtons[mode].setAttribute(
      'aria-expanded',
      String(genModelListOpen && genState.mode === mode)
    );
  }
  for (const button of genVmodeRow.querySelectorAll('.gen-seg-btn')) {
    const active = button.dataset.vmode === genState.video.vmode;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
    const videoModel = genSelectedModel('video');
    const imageCount = selectedImageItems().length;
    let enabled = true;
    let hint = '';
    if (button.dataset.vmode === 'frames') {
      // Frames (i2v): 1-2 ảnh làm frame đầu/cuối — chỉ model hỗ trợ i2v.
      enabled = videoModel?.frames === true && imageCount >= 1 && imageCount <= 2;
      hint = videoModel?.frames === false
        ? 'Model này không hỗ trợ Frames to Video'
        : 'Chọn 1 ảnh (frame đầu) hoặc 2 ảnh (frame đầu + cuối) trong stage.';
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
  genLabel.textContent = `${model.label} · ${state.ratio} · x${state.qty}`;
}

function setGenDialogOpen(open) {
  genDialog.hidden = !open;
  genTrigger.setAttribute('aria-expanded', String(open));
  genModelListOpen = false;
}

function setGenMode(mode) {
  if (!genBodies[mode]) return;
  genState.mode = mode;
  genModelListOpen = false;
  renderGenDialog();
}

genTrigger.addEventListener('click', () => setGenDialogOpen(genDialog.hidden));
for (const tab of genTabs) {
  tab.addEventListener('click', () => setGenMode(tab.dataset.mode));
}
for (const button of genVmodeRow.querySelectorAll('.gen-seg-btn')) {
  button.addEventListener('click', () => {
    const videoModel = genSelectedModel('video');
    const imageCount = selectedImageItems().length;
    if (button.dataset.vmode === 'frames') {
      if (videoModel?.frames !== true) {
        showToast('Model này không hỗ trợ Frames to Video.');
        return;
      }
      if (imageCount < 1 || imageCount > 2) {
        showToast('Chọn 1 ảnh (frame đầu) hoặc 2 ảnh (frame đầu + cuối) trong stage.');
        return;
      }
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
  genState.video.length = Number(genLengthSlider.value);
  genLengthValue.textContent = `${genState.video.length} Sec`;
  for (const tick of genLengthTicks.children) {
    tick.classList.toggle('is-active', Number(tick.dataset.value) === genState.video.length);
  }
});

function buildGeneratePayload(promptText) {
  const mode = genState.mode;
  const state = genState[mode];
  const model = genSelectedModel(mode);
  // Ảnh tham chiếu lấy từ selection trong stage (thứ tự chọn), chỉ item ảnh
  // còn tồn tại; cap theo mode/vmode (image 10, Frames 2, Ingredients 3).
  const selectedImages = selectedImageItems().map((item) => String(item.id));
  const vmode = mode === 'video' ? state.vmode : 'text';
  let references = [];
  if (mode === 'image') {
    references = model?.refs === true ? selectedImages.slice(0, MAX_REFS.image) : [];
  } else if (vmode === 'frames' && model?.frames === true) {
    references = selectedImages.slice(0, 2);
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

async function ensureActiveProject() {
  const existing = projects.find(({ id }) => id === activeProject);
  if (existing) return existing;
  if (creatingProject) return null;
  // Mirror the New project handler so sending always has a target project.
  let number = 1;
  const names = new Set(projects.map(({ name }) => name));
  while (names.has(`Project ${number}`)) number += 1;
  creatingProject = true;
  try {
    const response = await request('/playground/api/projects', {
      method: 'POST',
      body: JSON.stringify({ name: `Project ${number}`, prompt: prompt.value, results: [] }),
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
  const project = await ensureActiveProject();
  if (!project) return;
  if (jobBusy(project.id)) {
    showToast('Đang tạo nội dung, vui lòng chờ.');
    return;
  }
  await launchGeneration(project, buildGeneratePayload(promptText), { clearPrompt: true });
}

// Đường chung cho generate từ composer lẫn Extend: đặt pending → POST → poll.
async function launchGeneration(project, payload, { clearPrompt = false } = {}) {
  pendingByProject.set(project.id, {
    mode: payload.mode,
    ratio: payload.ratio,
    count: payload.qty,
    startedAt: Date.now(),
  });
  submitting.add(project.id);
  renderResultsGrid();
  syncComposerState();
  let response;
  try {
    response = await request(projectPath(project.id, '/generate'), {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  } catch {
    clearJobState(project.id);
    renderResultsGrid();
    syncComposerState();
    showToast('Không kết nối được với máy chủ.');
    return false;
  }
  if (response.status === 401 || response.status === 403) {
    // Session expired: back to the key screen.
    clearJobState(project.id);
    setSignedIn(false);
    keyInput.focus();
    return false;
  }
  if (!response.ok) {
    let message = 'Không tạo được nội dung.';
    try {
      const data = await response.json();
      if (data?.detail) message = String(data.detail);
    } catch { /* Keep the generic message. */ }
    clearJobState(project.id);
    renderResultsGrid();
    syncComposerState();
    showToast(message);
    return false;
  }
  let jobId = null;
  try {
    jobId = (await response.json())?.job_id ?? null;
  } catch {
    jobId = null;
  }
  submitting.delete(project.id);
  if (!jobId) {
    clearJobState(project.id);
    renderResultsGrid();
    syncComposerState();
    showToast('Không tạo được nội dung.');
    return false;
  }
  activeJobs.set(project.id, jobId);
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
  startPolling(project.id, jobId);
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
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
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
  if (!paintEditor.hidden) {
    event.preventDefault();
    closePaintEditor();
    return;
  }
  if (!maskEditor.hidden) {
    event.preventDefault();
    closeMaskEditor();
    return;
  }
  if (!extendDialog.hidden) {
    event.preventDefault();
    closeExtendDialog();
    return;
  }
  if (!wmModal.hidden) {
    event.preventDefault();
    wmModal.hidden = true;
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
  keyInput.focus();
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

// --- Items: rename / tham chiếu / xóa / bôi vùng làm tham chiếu --------------

const paintEditor = document.querySelector('#paint-editor');
const paintCanvas = document.querySelector('#paint-canvas');
const paintBrush = document.querySelector('#paint-brush');
const paintReset = document.querySelector('#paint-reset');
const paintCancel = document.querySelector('#paint-cancel');
const paintSave = document.querySelector('#paint-save');
const itemDialog = document.querySelector('#item-dialog');
const itemDialogForm = document.querySelector('#item-dialog-form');
const itemDialogTitle = document.querySelector('#item-dialog-title');
const itemNameInput = document.querySelector('#item-name');
const itemDialogError = document.querySelector('#item-dialog-error');
const itemDialogCancel = document.querySelector('#item-dialog-cancel');
const itemDialogSubmit = document.querySelector('#item-dialog-submit');

let openResultMenu = null; // { toggle, actions, projectId, itemId }
let paintState = null; // { projectId, item, img, strokes, dirty }
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

async function createReferenceItem(projectId, { name, image }) {
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
    showToast('Đã thêm ảnh tham chiếu.');
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

function openResultMenuFor(toggle, item) {
  const projectId = activeProject;
  if (!projectId) return;
  const actions = document.createElement('div');
  actions.className = 'result-menu-list';
  const buttons = [];
  const renameBtn = makeButton('result-menu-item', 'Đổi tên');
  buttons.push(renameBtn);
  let paintBtn = null;
  let maskBtn = null;
  let extendBtn = null;
  if (item.kind === 'image') {
    const isRef = item.role === 'reference';
    const refToggle = makeButton('result-menu-item', isRef ? 'Bỏ đánh dấu tham chiếu' : 'Đổi thành tham chiếu');
    paintBtn = makeButton('result-menu-item', 'Bôi vùng làm tham chiếu');
    if (item.meta?.model && item.meta?.ratio) {
      maskBtn = makeButton('result-menu-item', 'Tạo mask WM từ ảnh này');
    }
    buttons.push(refToggle, paintBtn, ...(maskBtn ? [maskBtn] : []));
  } else if (item.kind === 'video' && item.meta?.media_generation_id) {
    // Chỉ video tạo trong playground mới có media id để nối tiếp (Extend).
    extendBtn = makeButton('result-menu-item', 'Nối tiếp video này (Extend)');
    buttons.push(extendBtn);
  }
  const deleteBtn = makeButton('result-menu-item is-danger', 'Xóa');
  buttons.push(deleteBtn);
  actions.append(...buttons);
  document.body.append(actions);
  positionMenu(actions, toggle.getBoundingClientRect());

  renameBtn.addEventListener('click', () => {
    closeResultMenu();
    openItemDialog('rename', projectId, item);
  });
  if (paintBtn) {
    const refToggle = buttons[1];
    refToggle.addEventListener('click', () => {
      closeResultMenu();
      // Selection là nguồn sự thật cho role — đổi selection là đủ, sync
      // role chạy ngầm theo debounce.
      setSelected(item.id, !isSelected(item.id));
    });
    paintBtn.addEventListener('click', () => {
      closeResultMenu();
      openPaintEditor(projectId, item);
    });
    if (maskBtn) {
      maskBtn.addEventListener('click', () => {
        closeResultMenu();
        openMaskEditor(item);
      });
    }
  }
  if (extendBtn) {
    extendBtn.addEventListener('click', () => {
      closeResultMenu();
      openExtendDialog(projectId, item);
    });
  }
  deleteBtn.addEventListener('click', () => {
    closeResultMenu();
    openItemDialog('delete', projectId, item);
  });

  openResultMenu = { toggle, actions, projectId, itemId: String(item.id) };
  toggle.setAttribute('aria-expanded', 'true');
}

// ── Paint editor ─────────────────────────────────────────────────────────────

function paintRedraw() {
  if (!paintState) return;
  const { img, strokes } = paintState;
  const ctx = paintCanvas.getContext('2d');
  ctx.clearRect(0, 0, paintCanvas.width, paintCanvas.height);
  ctx.drawImage(img, 0, 0, paintCanvas.width, paintCanvas.height);
  const brush = Number(paintBrush.value);
  ctx.strokeStyle = 'rgba(215, 244, 125, 0.65)';
  ctx.fillStyle = 'rgba(215, 244, 125, 0.4)';
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
  paintSave.disabled = strokes.length === 0;
}

async function openPaintEditor(projectId, item) {
  paintState = null;
  paintEditor.hidden = false;
  try {
    const response = await fetch(item.url, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('load failed');
    const blob = await response.blob();
    const bitmap = await createImageBitmap(blob);
    paintState = { projectId, item, img: bitmap, strokes: [] };
    paintCanvas.width = bitmap.width;
    paintCanvas.height = bitmap.height;
    paintRedraw();
    paintCanvas.focus?.();
  } catch {
    paintEditor.hidden = true;
    showToast('Không tải được ảnh để bôi vùng.');
  }
}

function closePaintEditor() {
  paintState = null;
  paintEditor.hidden = true;
}

function paintCanvasPoint(event) {
  const rect = paintCanvas.getBoundingClientRect();
  const scaleX = paintCanvas.width / rect.width;
  const scaleY = paintCanvas.height / rect.height;
  return {
    x: (event.clientX - rect.left) * scaleX,
    y: (event.clientY - rect.top) * scaleY,
  };
}

paintCanvas.addEventListener('pointerdown', (event) => {
  if (!paintState) return;
  event.preventDefault();
  paintCanvas.setPointerCapture(event.pointerId);
  const stroke = [paintCanvasPoint(event)];
  paintState.strokes.push(stroke);
  paintRedraw();
});
paintCanvas.addEventListener('pointermove', (event) => {
  if (!paintState || !paintState.strokes.length) return;
  if (!(event.buttons & 1)) return;
  paintState.strokes[paintState.strokes.length - 1].push(paintCanvasPoint(event));
  paintRedraw();
});
paintCanvas.addEventListener('pointerup', () => paintRedraw());

paintBrush.addEventListener('input', paintRedraw);
paintReset.addEventListener('click', () => {
  if (!paintState) return;
  paintState.strokes = [];
  paintRedraw();
});
paintCancel.addEventListener('click', closePaintEditor);
paintSave.addEventListener('click', async () => {
  if (!paintState || !paintState.strokes.length) return;
  const { projectId, item, img, strokes } = paintState;
  // Bounding box của toàn bộ nét bôi + đệm nhỏ → crop chính vùng được chọn.
  const points = strokes.flat();
  const minX = Math.min(...points.map((p) => p.x));
  const maxX = Math.max(...points.map((p) => p.x));
  const minY = Math.min(...points.map((p) => p.y));
  const maxY = Math.max(...points.map((p) => p.y));
  const pad = Math.max(8, Math.round(Math.min(img.width, img.height) * 0.02));
  const sx = Math.max(0, Math.floor(minX - pad));
  const sy = Math.max(0, Math.floor(minY - pad));
  const sw = Math.min(img.width - sx, Math.ceil(maxX - minX + pad * 2));
  const sh = Math.min(img.height - sy, Math.ceil(maxY - minY + pad * 2));
  if (sw <= 0 || sh <= 0) return;
  const crop = document.createElement('canvas');
  crop.width = sw;
  crop.height = sh;
  crop.getContext('2d').drawImage(img, sx, sy, sw, sh, 0, 0, sw, sh);
  const dataUrl = crop.toDataURL('image/png');
  closePaintEditor();
  const updated = await updateProjectItem(projectId, String(item.id), {
    image: dataUrl,
    role: 'reference',
  });
  if (updated) showToast('Item đã thành ảnh tham chiếu.');
});

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

let maskState = null; // { key, img, strokes }

function maskRedraw() {
  if (!maskState) return;
  const { img, strokes } = maskState;
  const ctx = maskCanvas.getContext('2d');
  ctx.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
  ctx.drawImage(img, 0, 0, maskCanvas.width, maskCanvas.height);
  const brush = Number(maskBrush.value);
  ctx.strokeStyle = 'rgba(255, 84, 84, 0.6)';
  ctx.fillStyle = 'rgba(255, 84, 84, 0.35)';
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

async function openMaskEditor(item) {
  // Mask key theo base model + tỉ lệ: gen-options trả base cho từng model id.
  const spec = optionsFor('image')?.models?.find((model) => model.id === item.meta?.model)
    ?? optionsFor('video')?.models?.find((model) => model.id === item.meta?.model);
  const base = spec?.base;
  const ratio = item.meta?.ratio;
  if (!base || !ratio) {
    showToast('Ảnh này thiếu thông tin model/ratio — không tạo được mask.');
    return;
  }
  maskState = null;
  maskTitle.textContent = `Tô vùng WM cần xoá — ${base} · ${ratio}`;
  maskEditor.hidden = false;
  try {
    const response = await fetch(item.url, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('load failed');
    const bitmap = await createImageBitmap(await response.blob());
    maskState = { key: `${base}:${ratio}`, img: bitmap, strokes: [] };
    maskCanvas.width = bitmap.width;
    maskCanvas.height = bitmap.height;
    maskRedraw();
  } catch (err) {
    maskEditor.hidden = true;
    showToast(`Không tải được ảnh để tạo mask: ${err?.message || err}`);
  }
}

function closeMaskEditor() {
  maskState = null;
  maskEditor.hidden = true;
}

maskCanvas.addEventListener('pointerdown', (event) => {
  if (!maskState) return;
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
  maskState.strokes = [];
  maskRedraw();
});
maskCancel.addEventListener('click', closeMaskEditor);
maskSave.addEventListener('click', async () => {
  if (!maskState || !maskState.strokes.length) return;
  const { key, img, strokes } = maskState;
  // Mask export: nền đen + nét trắng đục cùng brush — server chuẩn hoá 0/255.
  const out = document.createElement('canvas');
  out.width = img.width;
  out.height = img.height;
  const ctx = out.getContext('2d');
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, out.width, out.height);
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
  } catch {
    showToast('Không lưu được mask.');
  }
});

// ── WM settings modal (danh sách mask + engine) ─────────────────────────────

const wmModal = document.querySelector('#wm-modal');
const wmEngine = document.querySelector('#wm-engine');
const wmMaskList = document.querySelector('#wm-mask-list');
const wmClose = document.querySelector('#wm-close');

wmSettingsButton.addEventListener('click', () => {
  wmModal.hidden = false;
  loadWmInfo();
});
wmClose.addEventListener('click', () => { wmModal.hidden = true; });
wmModal.addEventListener('click', (event) => {
  if (event.composedPath()[0] === wmModal) wmModal.hidden = true;
});

async function loadWmInfo() {
  try {
    const response = await request('/playground/api/watermark');
    if (!response.ok) throw new Error('load failed');
    const info = await response.json();
    const engine = typeof info.engine === 'string' ? info.engine : 'disabled';
    wmEngine.textContent = info.enabled
      ? `Bật · engine: ${engine} · ${Array.isArray(info.masks) ? info.masks.length : 0} mask`
      : 'Đang tắt (WATERMARK_ENABLED=0)';
    wmMaskList.replaceChildren(
      ...(Array.isArray(info.masks) ? info.masks : []).map((mask) => {
        const row = document.createElement('li');
        row.className = 'wm-mask-row';
        const name = document.createElement('span');
        name.className = 'wm-mask-key';
        name.textContent = mask.key === '__global__' ? 'watermark.png (dùng chung)' : mask.key;
        const remove = makeButton('wm-mask-delete', 'Xóa');
        remove.addEventListener('click', async () => {
          try {
            const response = await request(
              `/playground/api/watermark/mask/${encodeURIComponent(mask.key)}`,
              { method: 'DELETE' },
            );
            if (response.ok || response.status === 404) {
              loadWmInfo();
              return;
            }
            showToast('Không xóa được mask.');
          } catch {
            showToast('Không xóa được mask.');
          }
        });
        row.append(name, remove);
        return row;
      })
    );
  } catch {
    wmEngine.textContent = 'Không tải được trạng thái watermark.';
  }
}

// ── Extend: nối tiếp video đã tạo bằng prompt ───────────────────────────────

const extendDialog = document.querySelector('#extend-dialog');
const extendForm = document.querySelector('#extend-form');
const extendPrompt = document.querySelector('#extend-prompt');
const extendError = document.querySelector('#extend-error');
const extendCancel = document.querySelector('#extend-cancel');
const extendSubmit = document.querySelector('#extend-submit');

let extendState = null; // { projectId, item }

function openExtendDialog(projectId, item) {
  extendState = { projectId, item };
  extendError.hidden = true;
  extendPrompt.value = '';
  extendSubmit.disabled = false;
  const ratio = item.meta?.ratio ? ` · ${item.meta.ratio}` : '';
  extendDialog.querySelector('#extend-title').textContent =
    `Nối tiếp video này${ratio}`;
  extendDialog.hidden = false;
  extendPrompt.focus();
}

function closeExtendDialog() {
  extendState = null;
  extendDialog.hidden = true;
}

extendCancel.addEventListener('click', closeExtendDialog);
extendDialog.addEventListener('click', (event) => {
  if (event.composedPath()[0] === extendDialog) closeExtendDialog();
});
extendForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!extendState || extendSubmit.disabled) return;
  const { projectId, item } = extendState;
  const promptText = extendPrompt.value.trim();
  if (!promptText) {
    extendError.textContent = 'Prompt không được để trống.';
    extendError.hidden = false;
    return;
  }
  if (jobBusy(projectId)) {
    extendError.textContent = 'Đang có job chạy — vui lòng đợi job hiện tại xong.';
    extendError.hidden = false;
    return;
  }
  extendSubmit.disabled = true;
  const payload = {
    mode: 'video',
    model: 'veo-3.1-lite',
    ratio: item.meta?.ratio ?? '16:9',
    qty: 1,
    res: null,
    length: null,
    vmode: 'extend',
    extend_item_id: String(item.id),
    references: [],
    prompt: promptText,
  };
  closeExtendDialog();
  const launched = await launchGeneration(
    projects.find(({ id }) => id === projectId),
    payload,
    { clearPrompt: false },
  );
  if (launched) showToast('Đang tạo đoạn tiếp theo…');
});
