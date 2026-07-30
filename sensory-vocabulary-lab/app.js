"use strict";

const app = document.querySelector("#app");
const APP_BASE_PATH = new URL(".", document.baseURI).pathname.replace(/\/?$/, "/");

const STAGES = [
  { label: "画面", hint: "先让眼睛接住它" },
  { label: "感受", hint: "说出身体的第一反应" },
  { label: "揭晓", hint: "把画面扣回词义" },
  { label: "例句", hint: "放进真实语境" },
];

const ICONS = {
  leaf: `<svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><path d="M16 28c.2-9.8 2.9-17.2 10.2-22.8M17.3 20.8c5.2.4 8.8-2.2 9.8-7.7-5.1-.6-8.8 2-9.8 7.7ZM13.9 24.1c-5 .5-8.3-2-9.2-7.2 4.9-.7 8.2 1.8 9.2 7.2ZM18.7 15c-3.5-2.5-4-6.3-1.6-10.1 3.7 2.4 4.3 6.1 1.6 10.1Z" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  plus: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  arrowRight: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  arrowLeft: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19 12H5m5-5-5 5 5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  volume: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 10v4h3l4 4V6l-4 4H5Zm10.3-.8a4 4 0 0 1 0 5.6M18 6.5a8 8 0 0 1 0 11" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  refresh: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M19 8V4m0 0h-4m4 0-3.1 3.1a7 7 0 1 0 1.4 8" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  game: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8.2 8h7.6c2.5 0 4.6 1.8 5 4.3l.7 4.2a2.7 2.7 0 0 1-4.6 2.4l-2.2-2.2H9.3l-2.2 2.2a2.7 2.7 0 0 1-4.6-2.4l.7-4.2A5 5 0 0 1 8.2 8Z" stroke="currentColor" stroke-width="1.7"/><path d="M7 11v4m-2-2h4m7.5-1h.01m2 2h.01" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
};

const state = {
  health: null,
  access: { configured: true, authenticated: true },
  persistentLibrary: false,
  demo: null,
  savedPacks: [],
  legacyPacks: [],
  legacyImported: false,
  packsOffset: 0,
  hasMorePacks: false,
  view: "home",
  activePack: null,
  wordIndex: 0,
  stage: 0,
  sceneByWord: {},
  feelingAnswers: {},
  imageJobs: {},
  modal: null,
  formError: "",
  busy: null,
  toast: null,
  guessIndex: 0,
  guessReveal: false,
  guessResult: "",
  multiReveal: false,
  accessError: "",
};

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function appPath(value) {
  return `${APP_BASE_PATH}${String(value ?? "").replace(/^\/+/, "")}`;
}

function safeImage(value) {
  const url = String(value ?? "");
  if (/^\/assets\/[a-zA-Z0-9_./-]+$/.test(url) && !url.includes("..")) return appPath(url);
  if (/^\/api\/images\/[a-zA-Z0-9_-]+$/.test(url)) return appPath(url);
  return /^data:image\/(?:png|jpeg|webp);base64,[a-zA-Z0-9+/=]+$/.test(url) ? url : "";
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function icon(name, className = "") {
  const svg = ICONS[name] || "";
  return className ? svg.replace("<svg ", `<svg class="${className}" `) : svg;
}

function readSavedPacks() {
  try {
    const parsed = JSON.parse(localStorage.getItem("sensory-vocab-packs") || "[]");
    return Array.isArray(parsed) ? parsed.slice(0, 8) : [];
  } catch {
    return [];
  }
}

function savePacks() {
  try {
    const packs = state.savedPacks.slice(0, 8).map((pack) => {
      const stored = clone(pack);
      for (const word of stored.words || []) {
        for (const scene of word.scenes || []) {
          if (String(scene.image || "").startsWith("data:image/")) scene.image = null;
        }
      }
      return stored;
    });
    localStorage.setItem("sensory-vocab-packs", JSON.stringify(packs));
  } catch {
    showToast("浏览器没有足够空间保存这个词包，但本次仍可继续学习。", 4200);
  }
}

function persistActivePack() {
  if (!state.activePack || state.activePack.source !== "live") return;
  if (state.persistentLibrary) return;
  const existing = state.savedPacks.findIndex((pack) => pack.id === state.activePack.id);
  if (existing >= 0) state.savedPacks[existing] = clone(state.activePack);
  else state.savedPacks.unshift(clone(state.activePack));
  savePacks();
}

async function apiFetch(path, options = {}) {
  const response = await fetch(appPath(path), {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error("服务器返回了无法读取的内容。请确认是通过 server.py 打开的页面。 ");
  }
  if (!response.ok) {
    const error = new Error(payload?.error?.message || "请求失败，请稍后重试。 ");
    error.code = payload?.error?.code;
    error.retryable = payload?.error?.retryable !== false;
    error.details = payload?.error?.details || {};
    if (response.status === 401 && error.code === "access_required") {
      state.access.authenticated = false;
    }
    throw error;
  }
  return payload;
}

function renderAccessGate() {
  document.title = "进入词汇感官实验室";
  const configured = state.access?.configured !== false;
  return `<main class="access-shell">
    <section class="access-card" aria-labelledby="access-title">
      <div class="access-mark">${icon("leaf")}</div>
      <span class="eyebrow">Private vocabulary garden</span>
      <h1 id="access-title">先打开这间<br />视觉实验室。</h1>
      <p>输入站点访问密钥。验证只在服务端完成，密钥不会保存在浏览器或写进词包。</p>
      ${configured ? `<form id="access-form" class="access-form">
        <label for="access-key">访问密钥</label>
        <div class="access-input-row"><input id="access-key" name="key" type="password" autocomplete="current-password" required autofocus placeholder="••••••••••••••••••••" /><button class="primary-button" type="submit">进入实验室 ${icon("arrowRight", "button-icon")}</button></div>
        ${state.accessError ? `<p class="form-error">${esc(state.accessError)}</p>` : ""}
      </form>` : `<div class="access-config-error"><strong>站点还没有配置访问密钥</strong><p>请维护者设置 <code>SENSORY_VOCAB_ACCESS_KEY_SHA256</code> 后重新部署。</p></div>`}
      <footer><span>固定密钥访问</span><span>HttpOnly 安全会话</span><span>共享词库</span></footer>
    </section>
    <aside class="access-sketch" aria-hidden="true">
      <div class="access-word">feel</div>
      <div class="access-arrow">↘</div>
      <div class="access-frame"><span>先看见</span><i></i><b></b></div>
      <p>词义在门后，<br />画面在眼前。</p>
    </aside>
  </main>`;
}

function modePill() {
  const live = state.health?.mode === "live";
  return `<span class="mode-pill ${live ? "live" : ""}" title="${
    live ? `语义：${esc(state.health.textModel)}；图片：${esc(state.health.imageModel)}` : "未配置本机 API Key，当前使用内置样图"
  }"><i class="mode-dot"></i><span>${live ? "实时生成已连接" : "原创样图模式"}</span></span>`;
}

function brandButton() {
  return `<button class="brand" data-action="home" aria-label="返回词汇感官实验室首页">
    <span class="brand-mark">${icon("leaf")}</span><span>词汇感官实验室</span>
  </button>`;
}

function homeHeader() {
  return `<header class="site-header">
    ${brandButton()}
    <div class="header-actions">
      ${modePill()}
      ${state.persistentLibrary ? `<button class="text-button" data-action="logout">退出密钥</button>` : ""}
      <button class="primary-button" data-action="open-create">${icon("plus", "button-icon")}<span>新建词包</span></button>
    </div>
  </header>`;
}

function compactHeader() {
  return `<header class="site-header">
    ${brandButton()}
    <div class="header-actions">
      ${modePill()}
      <button class="icon-button" data-action="home" aria-label="关闭并返回首页">${icon("close")}</button>
    </div>
  </header>`;
}

function allPacks() {
  return [state.demo, ...state.savedPacks].filter(Boolean);
}

function packCard(pack, index) {
  const sample = pack.words.slice(0, 3).map((item) => item.word).join(" · ");
  return `<button class="pack-card" data-action="open-pack" data-pack-id="${esc(pack.id)}" data-index="${String(index + 1).padStart(2, "0")}">
    <span class="pack-meta"><span class="level-tag">${esc(pack.level)}</span><span>${esc(pack.date || "刚刚")}</span></span>
    <h3>${esc(pack.title)}</h3>
    <p>${esc(sample)}</p>
    <span class="pack-footer"><span>${pack.words.length} 个词 · ${pack.source === "live" ? (state.persistentLibrary ? "共享词包" : "实时词包") : "内置示范"}</span><span class="arrow-doodle">↗</span></span>
  </button>`;
}

function renderHome() {
  document.title = "词汇感官实验室";
  const demoPour = safeImage(state.demo?.words?.[0]?.scenes?.[0]?.image);
  const demoIndividual = safeImage(state.demo?.words?.[1]?.scenes?.[0]?.image);
  const cards = allPacks().map(packCard).join("");
  const importNotice = state.persistentLibrary && state.legacyPacks.length && !state.legacyImported
    ? `<aside class="import-notice"><div><span class="micro-label">发现旧版内容</span><h3>浏览器里还有 ${state.legacyPacks.length} 个本地词包</h3><p>可以把文字和仍可读取的图片并入共享词库；已有共享词条不会被覆盖。</p></div><div class="import-actions"><button class="ghost-button" data-action="dismiss-import">暂不导入</button><button class="secondary-button" data-action="import-legacy">确认导入</button></div></aside>`
    : "";
  return `<div class="site-shell">
    ${homeHeader()}
    <main class="home-main">
      <section class="hero">
        <div class="hero-copy-block">
          <span class="eyebrow">Vocabulary, felt first</span>
          <h1>先<em>看见</em>，<br />再记住一个词。</h1>
          <p class="hero-copy">这里不把释义塞进脑袋。每个词先变成一个准确、必要时有点荒诞的手绘瞬间：方向、重量、距离和关系，都会先被身体感觉到。</p>
          <div class="hero-actions">
            <button class="primary-button" data-action="open-pack" data-pack-id="${esc(state.demo?.id)}">开始三词体验 ${icon("arrowRight", "button-icon")}</button>
            <button class="secondary-button" data-action="open-create">自己输入单词</button>
          </div>
          <p class="hero-note">不需要 API Key 也能完整体验 pour、individual、transfer。</p>
        </div>
        <div class="hero-collage" aria-label="原创手绘词义场景预览">
          <figure class="collage-card main">${demoPour ? `<img src="${demoPour}" alt="天空巨盆翻扣，蓝色雨水整股向下倾泻的手绘画" />` : ""}</figure>
          <figure class="collage-card small">${demoIndividual ? `<img src="${demoIndividual}" alt="人群与独立个体的手绘场景" />` : ""}</figure>
          <div class="collage-note">不是画单词，<br />是画它的<br /><strong>物理内核。</strong></div>
        </div>
      </section>

      <section aria-labelledby="packs-heading">
        <div class="section-heading">
          <h2 id="packs-heading">${state.persistentLibrary ? "共享词包库" : "我的词包"}</h2>
          <p>${state.persistentLibrary ? "同一个词在相同学习级别只生成一次。新词包遇到旧词，会直接接回已经保存的文字与画面。" : "每个词走过四步：看画面、说感受、揭开词义、放进例句；完成后再用两种小游戏检查视觉记忆。"}</p>
        </div>
        ${importNotice}
        <div class="pack-grid">
          ${cards}
          <button class="empty-card" data-action="open-create"><span class="plus-doodle">+</span><span>新建一个词包</span></button>
        </div>
        ${state.persistentLibrary && state.hasMorePacks ? `<div class="load-more-row"><button class="secondary-button" data-action="load-more-packs">加载更早的词包</button></div>` : ""}
      </section>
    </main>
    <footer class="home-footer"><p>原创视觉系统 · 单幅手绘 · 一图一义</p><p>${state.persistentLibrary ? "文字与图片持久保存 · 模型密钥不进入浏览器" : "本机优先，API Key 不进入浏览器"}</p></footer>
    ${renderOverlays()}
  </div>`;
}

function currentWord() {
  return state.activePack?.words?.[state.wordIndex];
}

function currentScene(word = currentWord()) {
  const sceneIndex = state.sceneByWord[word.id] || 0;
  return word.scenes[Math.min(sceneIndex, word.scenes.length - 1)];
}

function stageRail() {
  return `<nav class="stage-rail" aria-label="学习阶段">
    ${STAGES.map(
      (stage, index) => `<button class="stage-tab ${index === state.stage ? "active" : ""}" data-action="set-stage" data-stage="${index}" title="${esc(stage.hint)}"><strong>${index + 1}</strong>${esc(stage.label)}</button>`,
    ).join("")}
  </nav>`;
}

function imageJobFor(word, scene) {
  return state.imageJobs[`${word.id}:${scene.id}`];
}

function renderGenerationPlaceholder(job) {
  const phases = ["理解词义", "发明隐喻", "落笔生成"];
  if (job?.error) {
    const retry = job.retryable === false
      ? ""
      : `<button class="secondary-button" data-action="retry-image" style="margin-top:20px">再试一次</button>`;
    return `<div class="generator-placeholder"><div class="generator-sketch"></div><h3>这张画没有生成成功</h3><p>${esc(job.error)}</p>${retry}</div>`;
  }
  const phase = job?.phase ?? 0;
  return `<div class="generator-placeholder"><div class="generator-sketch"></div><h3>${job ? phases[phase] : "准备画纸"}…</h3><p>场景导演先找物理内核，再把一个视觉比喻交给画笔。</p><ol class="generation-steps">${phases
    .map((label, index) => `<li class="${index < phase ? "done" : index === phase ? "active" : ""}">${index < phase ? "✓ " : ""}${label}</li>`)
    .join("")}</ol></div>`;
}

function renderImageStage(word) {
  const sceneIndex = state.sceneByWord[word.id] || 0;
  const scene = word.scenes[sceneIndex] || word.scenes[0];
  const source = state.activePack.source === "live"
    ? (state.persistentLibrary ? "共享词库手绘" : "AI 实时手绘")
    : "原创内置样图";
  const image = safeImage(scene.image);
  const job = imageJobFor(word, scene);
  const sceneButtons = word.scenes.length > 1
    ? `<div class="scene-switcher" aria-label="切换视觉场景">${word.scenes
        .map(
          (_, index) => `<button class="scene-dot ${index === sceneIndex ? "active" : ""}" data-action="select-scene" data-scene="${index}">场景 ${index + 1}</button>`,
        )
        .join("")}</div>`
    : "";

  queueMicrotask(() => {
    if (!scene.image && !job) ensureSceneImage(state.wordIndex, sceneIndex);
  });

  return `<article class="lesson-card image-stage">
    <div class="visual-frame">
      <span class="figure-stamp">${esc(source)}</span>
      ${image ? `<img src="${image}" alt="${esc(scene.captionCn)}" />` : renderGenerationPlaceholder(job)}
    </div>
    <aside class="image-aside">
      <span class="lesson-kicker">Stage 01 · 不翻译</span>
      <h2>先看三秒，<br />身体发生了什么？</h2>
      <p>别急着找中文。先注意方向、重量、速度、距离，或者谁和谁的关系发生了变化。</p>
      ${sceneButtons}
      <div class="aside-tools">
        <button class="ghost-button" data-action="regenerate-image" ${state.health?.mode !== "live" ? "disabled" : ""} title="${state.health?.mode === "live" ? "用同一语义重新构图" : "配置 OPENROUTER_API_KEY 后可重新构图"}">${icon("refresh", "button-icon")}重新构图</button>
        ${state.persistentLibrary && state.activePack.source === "live" ? `<button class="text-button danger-text" data-action="regenerate-entry">重做整个词条</button>` : ""}
      </div>
      ${state.persistentLibrary && Number(word.referenceCount || 0) > 1 ? `<p class="shared-reference-note">这个词被 ${Number(word.referenceCount)} 个词包共用。重新生成会同步更新所有引用。</p>` : ""}
    </aside>
  </article>`;
}

function feelingKey(word) {
  return `${state.activePack.id}:${word.id}`;
}

function renderFeelingStage(word) {
  const answer = state.feelingAnswers[feelingKey(word)] || "";
  return `<article class="lesson-card text-stage">
    <div class="text-stage-inner">
      <span class="stage-number">2</span>
      <span class="lesson-kicker">Stage 02 · 感受先于定义</span>
      <h2>${esc(word.sensoryPromptCn)}</h2>
      <p class="lead">没有标准答案。选一个接近的感觉，或者用自己的动作和话来描述。</p>
      <div class="chips">${(word.feelingChips || [])
        .map((chip) => `<button class="chip ${answer === chip ? "selected" : ""}" data-action="feel-chip" data-value="${esc(chip)}">${esc(chip)}</button>`)
        .join("")}</div>
      <label class="sr-only" for="feeling-answer">写下你的第一感觉</label>
      <textarea id="feeling-answer" class="answer-field" data-input="feeling" placeholder="比如：像一整股东西突然压下来…">${esc(answer)}</textarea>
    </div>
  </article>`;
}

function renderRevealStage(word) {
  const scene = currentScene(word);
  return `<article class="lesson-card text-stage">
    <div class="text-stage-inner">
      <span class="lesson-kicker">Stage 03 · 把感觉扣回词义</span>
      <div class="word-reveal"><h2>${esc(word.word)}</h2><span class="pos-tag">${esc(word.pos)}</span></div>
      <div class="phonetic-row"><span>${esc(word.phonetic)}</span><button class="icon-button" data-action="speak" aria-label="朗读 ${esc(word.word)}">${icon("volume")}</button></div>
      <p class="definition">${esc(word.coreSenseCn)}</p>
      <div class="usage-hook">${esc(scene.usageHookCn || "这幅画把词义里最关键的关系变成了可见动作。")}</div>
    </div>
  </article>`;
}

function highlightedExample(sentence, word) {
  const escaped = esc(sentence);
  const pattern = new RegExp(`(${escapeRegExp(esc(word))}[a-z]*)`, "ig");
  return escaped.replace(pattern, "<mark>$1</mark>");
}

function renderExampleStage(word) {
  return `<article class="lesson-card text-stage">
    <div class="text-stage-inner">
      <span class="lesson-kicker">Stage 04 · 回到真实语言</span>
      <h2>现在，让画面进入一句话。</h2>
      <div class="example-card">
        <span class="quote-mark" aria-hidden="true">“</span>
        <p class="example-en">${highlightedExample(word.exampleEn, word.word)}</p>
        <p class="example-zh">${esc(word.exampleZh)}</p>
        <div class="example-actions"><button class="secondary-button" data-action="speak-example">${icon("volume", "button-icon")}听完整例句</button></div>
      </div>
    </div>
  </article>`;
}

function lessonDock() {
  const atStart = state.wordIndex === 0 && state.stage === 0;
  const atEnd = state.wordIndex === state.activePack.words.length - 1 && state.stage === STAGES.length - 1;
  const nextLabel = atEnd ? "去玩小游戏" : state.stage === 3 ? "下一个词" : "继续";
  return `<footer class="lesson-dock"><div class="dock-inner">
    <button class="ghost-button" data-action="previous" aria-label="上一步" ${atStart ? "disabled" : ""}>${icon("arrowLeft", "button-icon")}<span>上一步</span></button>
    <div class="dock-dots" aria-label="当前阶段">${STAGES.map((_, index) => `<button class="dock-dot ${index === state.stage ? "active" : ""}" data-action="set-stage" data-stage="${index}" aria-label="第 ${index + 1} 阶段"></button>`).join("")}</div>
    <button class="primary-button" data-action="advance" aria-label="${nextLabel}"><span>${nextLabel}</span>${atEnd ? icon("game", "button-icon") : icon("arrowRight", "button-icon")}</button>
  </div></footer>`;
}

function renderLesson() {
  const word = currentWord();
  if (!word) {
    state.view = "home";
    return renderHome();
  }
  document.title = `${word.word} · 词汇感官实验室`;
  const cards = [renderImageStage, renderFeelingStage, renderRevealStage, renderExampleStage];
  return `<div class="learn-shell">
    <header class="learn-header">
      ${brandButton()}
      <div class="word-progress">WORD ${String(state.wordIndex + 1).padStart(2, "0")} / ${String(state.activePack.words.length).padStart(2, "0")}</div>
      <div class="learn-header-actions"><button class="ghost-button" data-action="open-games">${icon("game", "button-icon")}游戏</button><button class="icon-button" data-action="home" aria-label="关闭学习">${icon("close")}</button></div>
    </header>
    ${stageRail()}
    <main class="lesson-wrap">${cards[state.stage](word)}</main>
    ${lessonDock()}
    ${renderOverlays()}
  </div>`;
}

function renderGameMenu() {
  document.title = "词义小游戏 · 词汇感官实验室";
  return `<div class="site-shell">
    ${compactHeader()}
    <main class="game-main">
      <section class="game-hero"><span class="eyebrow">Visual memory check</span><h1>画面记住了吗？</h1><p>不考拼写表格。我们只检查：看到另一种场景时，你还能不能认出同一个语义骨架。</p></section>
      <div class="game-grid">
        <button class="game-card" data-action="play-guess"><span class="game-card-art"></span><span class="game-number">01</span><h2>场景猜词</h2><p>只给一幅手绘画。先说出你感到的动作或关系，再揭开对应单词。</p><span class="arrow-doodle">开始 ↗</span></button>
        <button class="game-card" data-action="play-multi"><span class="game-card-art"></span><span class="game-number">02</span><h2>一词多境</h2><p>两幅表面完全不同的画，共享哪一个核心变化？用差异逼出真正的词义。</p><span class="arrow-doodle">开始 ↗</span></button>
      </div>
    </main>
    ${renderOverlays()}
  </div>`;
}

function gameImage(word, scene, sceneIndex) {
  const image = safeImage(scene?.image);
  if (!image && !imageJobFor(word, scene)) {
    queueMicrotask(() => ensureSceneImage(state.activePack.words.indexOf(word), sceneIndex));
  }
  return image
    ? `<img src="${image}" alt="${esc(scene.captionCn)}" />`
    : renderGenerationPlaceholder(imageJobFor(word, scene));
}

function renderGuess() {
  const words = state.activePack.words;
  const word = words[state.guessIndex % words.length];
  const scene = word.scenes[0];
  document.title = "场景猜词 · 词汇感官实验室";
  return `<div class="site-shell">
    ${compactHeader()}
    <main class="game-main">
      <article class="play-card">
        <header class="play-topline"><h1>场景猜词</h1><span class="micro-label">${state.guessIndex + 1} / ${words.length}</span></header>
        <figure class="play-figure">${gameImage(word, scene, 0)}</figure>
        <section class="play-question">
          <h2>这幅画里的核心动作或关系，最像哪个英文词？</h2>
          <div class="inline-answer"><label class="sr-only" for="guess-answer">输入英文单词</label><input id="guess-answer" autocomplete="off" placeholder="输入你的答案…" /><button class="primary-button" data-action="reveal-guess">揭晓答案</button></div>
          ${state.guessReveal ? `<div class="reveal-panel"><strong>${esc(word.word)}</strong><p>${state.guessResult ? `<b>${esc(state.guessResult)}</b> ` : ""}${esc(word.coreSenseCn)}</p></div>` : ""}
        </section>
        <footer class="play-footer"><button class="ghost-button" data-action="open-games">${icon("arrowLeft", "button-icon")}游戏菜单</button><button class="secondary-button" data-action="next-guess" ${!state.guessReveal ? "disabled" : ""}>下一幅 ${icon("arrowRight", "button-icon")}</button></footer>
      </article>
    </main>
    ${renderOverlays()}
  </div>`;
}

function multiWord() {
  return state.activePack.words.find((word) => word.scenes?.length >= 2) || state.activePack.words[0];
}

function renderMulti() {
  const word = multiWord();
  const scenes = word.scenes.slice(0, 2);
  const usageHooks = scenes
    .map((scene) => String(scene.usageHookCn || "").replace(/[。；;]+$/u, ""))
    .filter(Boolean)
    .join("；");
  document.title = "一词多境 · 词汇感官实验室";
  return `<div class="site-shell">
    ${compactHeader()}
    <main class="game-main">
      <article class="play-card">
        <header class="play-topline"><h1>一词多境</h1><span class="micro-label">同一个语义骨架</span></header>
        <div class="dual-figures">${scenes.map((scene, index) => `<figure class="play-figure">${gameImage(word, scene, index)}</figure>`).join("")}</div>
        <section class="play-question">
          <h2>两幅画里，究竟是什么发生了同一种变化？</h2>
          <label class="sr-only" for="multi-answer">写下共同感觉</label><textarea id="multi-answer" class="answer-field" placeholder="别猜单词，先描述共同的方向、关系或变化…"></textarea>
          <div style="margin-top:12px"><button class="primary-button" data-action="reveal-multi">看核心答案</button></div>
          ${state.multiReveal ? `<div class="reveal-panel"><strong>${esc(word.word)}</strong><p>${esc(word.coreSenseCn)}</p><p style="margin-top:10px">↳ ${esc(usageHooks)}。</p></div>` : ""}
        </section>
        <footer class="play-footer"><button class="ghost-button" data-action="open-games">${icon("arrowLeft", "button-icon")}游戏菜单</button><button class="secondary-button" data-action="restart-multi" ${!state.multiReveal ? "disabled" : ""}>再看一次</button></footer>
      </article>
    </main>
    ${renderOverlays()}
  </div>`;
}

function renderCreateModal() {
  if (state.modal !== "create") return "";
  const demoOnly = state.health?.mode !== "live";
  return `<div class="modal-backdrop" data-action="close-modal">
    <section class="modal" role="dialog" aria-modal="true" aria-labelledby="create-title" data-modal-panel>
      <header class="modal-header"><h2 id="create-title">创建新词包</h2><button class="icon-button" data-action="close-modal" aria-label="关闭">${icon("close")}</button></header>
      <form class="create-form" id="create-form">
        <div class="form-row"><label for="word-list">输入单词列表 <span class="form-help">最多 8 个</span></label><textarea id="word-list" name="words" spellcheck="false" placeholder="每行一个，或用逗号分隔">pour
individual
transfer</textarea></div>
        <div class="form-row"><label for="level">学习级别</label><select id="level" name="level"><option>KET</option><option selected>PET</option><option>FCE</option><option>IELTS</option><option>TOEFL</option><option>GRE</option></select></div>
        ${demoOnly ? `<p class="demo-hint">现在是原创样图模式：pour、individual、transfer 可完整体验。要生成任意新词，请由站点维护者配置服务端模型。</p>` : state.persistentLibrary ? `<p class="demo-hint">系统会先检查共享词库。已保存的“单词 + 学习级别”会直接复用；只为缺失词生成新内容。</p>` : `<p class="demo-hint">场景策划通常几十秒完成；每张手绘图可能需要约一到两分钟，会在学习时按需生成并缓存。</p>`}
        ${state.formError ? `<p class="form-error">${esc(state.formError)}</p>` : ""}
        <div class="modal-actions"><button type="button" class="ghost-button" data-action="close-modal">取消</button><button type="submit" class="primary-button">解析并开始 ${icon("arrowRight", "button-icon")}</button></div>
      </form>
    </section>
  </div>`;
}

function renderOverlays() {
  return `${renderCreateModal()}${state.busy ? `<div class="busy-overlay"><div class="busy-card"><div class="ink-spinner"></div><h2>${esc(state.busy.title)}</h2><p>${esc(state.busy.detail)}</p></div></div>` : ""}${state.toast ? `<div class="toast" role="status">${esc(state.toast)}</div>` : ""}`;
}

function render() {
  if (state.persistentLibrary && !state.access.authenticated) {
    app.innerHTML = renderAccessGate();
    return;
  }
  const views = {
    home: renderHome,
    lesson: renderLesson,
    games: renderGameMenu,
    guess: renderGuess,
    multi: renderMulti,
  };
  app.innerHTML = (views[state.view] || renderHome)();
}

async function loadPacks({ append = false } = {}) {
  if (!state.persistentLibrary) return;
  const offset = append ? state.packsOffset : 0;
  const result = await apiFetch(`/api/packs?limit=24&offset=${offset}`);
  state.savedPacks = append
    ? [...state.savedPacks, ...(result.packs || [])]
    : (result.packs || []);
  state.packsOffset = state.savedPacks.length;
  state.hasMorePacks = Boolean(result.hasMore);
}

function replaceEntryEverywhere(entry) {
  const replaceInPack = (pack) => {
    if (!pack?.words) return;
    pack.words = pack.words.map((word) => word.id === entry.id ? clone(entry) : word);
  };
  for (const pack of state.savedPacks) replaceInPack(pack);
  replaceInPack(state.activePack);
}

async function regenerateCurrentEntry() {
  const word = currentWord();
  if (!word || !state.persistentLibrary) return;
  const references = Number(word.referenceCount || 1);
  const message = references > 1
    ? `“${word.word}”被 ${references} 个词包共用。重做会替换文字、两个场景和图片，并同步更新所有词包。继续吗？`
    : `重做“${word.word}”会替换文字、两个场景和图片。继续吗？`;
  if (!window.confirm(message)) return;
  state.busy = {
    title: `正在重做 ${word.word}`,
    detail: "旧内容会一直保留；只有新的文字和两张图全部完成后才会替换。",
  };
  render();
  try {
    const result = await apiFetch(`/api/entries/${encodeURIComponent(word.id)}/regenerate`, {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    replaceEntryEverywhere(result.entry);
    state.busy = null;
    showToast(`已同步更新 ${result.affectedPacks || 1} 个词包。`, 4200);
  } catch (error) {
    state.busy = null;
    showToast(`重做失败，旧词条没有改变：${error.message}`, 5200);
  }
}

async function importLegacyPacks() {
  if (!state.legacyPacks.length) return;
  state.busy = {
    title: "正在搬入旧词包",
    detail: "已有共享词条不会被覆盖；失效图片会保留为待生成状态。",
  };
  render();
  try {
    const result = await apiFetch("/api/import/legacy", {
      method: "POST",
      body: JSON.stringify({ packs: state.legacyPacks }),
    });
    localStorage.setItem("sensory-vocab-imported-v1", "1");
    state.legacyImported = true;
    await loadPacks();
    state.busy = null;
    showToast(`已导入 ${result.importedPacks} 个词包；${result.missingImages} 张旧图需要按需重画。`, 5200);
  } catch (error) {
    state.busy = null;
    showToast(`旧词包导入失败：${error.message}`, 5200);
  }
}

async function logout() {
  try {
    await apiFetch("/api/access", { method: "DELETE", body: "{}" });
  } catch {
    // Even if the network request fails, return the interface to the gate.
  }
  state.access.authenticated = false;
  state.activePack = null;
  state.savedPacks = [];
  state.view = "home";
  render();
}

async function waitForLibraryReady(initialHealth) {
  let health = initialHealth;
  for (let attempt = 0; health?.persistentLibrary && !health?.libraryReady && attempt < 20; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    health = await apiFetch("/api/health");
  }
  if (health?.persistentLibrary && !health?.libraryReady) {
    throw new Error("共享词库启动超时，请稍后刷新。");
  }
  return health;
}

function showToast(message, duration = 2800) {
  state.toast = message;
  render();
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    state.toast = null;
    render();
  }, duration);
}

function activatePack(pack) {
  state.activePack = clone(pack);
  state.wordIndex = 0;
  state.stage = 0;
  state.sceneByWord = {};
  state.imageJobs = {};
  state.view = "lesson";
  state.modal = null;
  state.guessIndex = 0;
  state.guessReveal = false;
  state.multiReveal = false;
  window.scrollTo({ top: 0, behavior: "instant" });
  render();
}

function openPack(packId) {
  const pack = allPacks().find((item) => item.id === packId);
  if (pack) activatePack(pack);
}

function goHome() {
  persistActivePack();
  state.view = "home";
  state.modal = null;
  state.busy = null;
  window.scrollTo({ top: 0, behavior: "instant" });
  render();
}

function advanceLesson() {
  if (state.stage < STAGES.length - 1) state.stage += 1;
  else if (state.wordIndex < state.activePack.words.length - 1) {
    state.wordIndex += 1;
    state.stage = 0;
  } else state.view = "games";
  render();
}

function previousLesson() {
  if (state.stage > 0) state.stage -= 1;
  else if (state.wordIndex > 0) {
    state.wordIndex -= 1;
    state.stage = STAGES.length - 1;
  }
  render();
}

function speak(text) {
  if (!("speechSynthesis" in window)) {
    showToast("这个浏览器暂不支持语音朗读。", 3500);
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  utterance.rate = 0.86;
  const voices = window.speechSynthesis.getVoices();
  utterance.voice = voices.find((voice) => voice.lang.startsWith("en") && /Samantha|Daniel|Karen|Google/.test(voice.name)) || voices.find((voice) => voice.lang.startsWith("en")) || null;
  window.speechSynthesis.speak(utterance);
}

async function ensureSceneImage(wordIndex, sceneIndex, regenerate = false) {
  const word = state.activePack?.words?.[wordIndex];
  const scene = word?.scenes?.[sceneIndex];
  if (!word || !scene || (scene.image && !regenerate)) return;
  const key = `${word.id}:${scene.id}`;
  if (state.imageJobs[key]?.loading) return;

  state.imageJobs[key] = { loading: true, phase: 0, error: "" };
  render();
  const timer = window.setInterval(() => {
    const job = state.imageJobs[key];
    if (!job?.loading) return window.clearInterval(timer);
    job.phase = Math.min(2, job.phase + 1);
    render();
  }, 1800);

  try {
    const result = await apiFetch("/api/generate-image", {
      method: "POST",
      body: JSON.stringify({
        sceneId: scene.id,
        regenerate,
      }),
    });
    const updateScene = (pack) => {
      for (const candidate of pack?.words || []) {
        const target = (candidate.scenes || []).find((item) => item.id === scene.id);
        if (target) {
          target.image = result.image;
          target.imageSource = result.source;
          target.imageRevision = Number(target.imageRevision || 0) + 1;
        }
      }
    };
    updateScene(state.activePack);
    for (const pack of state.savedPacks) updateScene(pack);
    delete state.imageJobs[key];
    persistActivePack();
    if (regenerate) {
      const affected = Number(result.affectedPacks || word.referenceCount || 1);
      showToast(state.persistentLibrary
        ? `新构图已保存，并同步到 ${affected} 个词包。`
        : "新构图已经画好并缓存到本机。");
    }
  } catch (error) {
    state.imageJobs[key] = {
      loading: false,
      phase: 0,
      error: error.message,
      retryable: error.retryable !== false,
    };
  } finally {
    window.clearInterval(timer);
    render();
  }
}

async function createPack(form) {
  const words = String(new FormData(form).get("words") || "")
    .split(/[\n,，;；]+/)
    .map((word) => word.trim())
    .filter(Boolean);
  const level = String(new FormData(form).get("level") || "PET");
  state.formError = "";
  state.busy = { title: "正在拆开词义", detail: "先找核心感觉，再设计两个视觉上不同的场景。" };
  render();

  try {
    const result = await apiFetch(state.persistentLibrary ? "/api/packs" : "/api/plan", {
      method: "POST",
      body: JSON.stringify({ words, level }),
    });
    const pack = result.pack;
    if (pack.source === "live") {
      state.savedPacks.unshift(clone(pack));
      if (!state.persistentLibrary) {
        state.savedPacks = state.savedPacks.slice(0, 8);
        savePacks();
      } else {
        state.packsOffset = state.savedPacks.length;
      }
    }
    state.busy = null;
    state.modal = null;
    if (state.persistentLibrary) {
      const hits = (result.cacheHits || []).length;
      const generated = (result.generatedWords || []).length;
      if (hits) {
        showToast(`已从共享词库读取 ${hits} 个词，只新生成 ${generated} 个。`, 4400);
      }
    }
    activatePack(pack);
  } catch (error) {
    state.busy = null;
    state.formError = error.message;
    state.modal = "create";
    render();
  }
}

app.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;

  if (action === "close-modal" && button.matches(".modal-backdrop") && event.target !== button) return;

  switch (action) {
    case "home":
      goHome();
      break;
    case "open-create":
      state.formError = "";
      state.modal = "create";
      render();
      requestAnimationFrame(() => document.querySelector("#word-list")?.focus());
      break;
    case "close-modal":
      state.modal = null;
      state.formError = "";
      render();
      break;
    case "open-pack":
      openPack(button.dataset.packId);
      break;
    case "logout":
      logout();
      break;
    case "load-more-packs":
      state.busy = { title: "正在翻找旧词包", detail: "从共享词库取回更早保存的内容。" };
      render();
      loadPacks({ append: true })
        .then(() => {
          state.busy = null;
          render();
        })
        .catch((error) => {
          state.busy = null;
          showToast(error.message, 4200);
        });
      break;
    case "import-legacy":
      importLegacyPacks();
      break;
    case "dismiss-import":
      state.legacyImported = true;
      render();
      break;
    case "set-stage":
      state.stage = Number(button.dataset.stage);
      render();
      break;
    case "advance":
      advanceLesson();
      break;
    case "previous":
      previousLesson();
      break;
    case "select-scene":
      state.sceneByWord[currentWord().id] = Number(button.dataset.scene);
      render();
      break;
    case "feel-chip":
      state.feelingAnswers[feelingKey(currentWord())] = button.dataset.value;
      render();
      break;
    case "speak":
      speak(currentWord().word);
      break;
    case "speak-example":
      speak(currentWord().exampleEn);
      break;
    case "regenerate-image": {
      const word = currentWord();
      const index = state.sceneByWord[word.id] || 0;
      if (state.persistentLibrary) {
        const references = Number(word.referenceCount || 1);
        const message = references > 1
          ? `这张图被 ${references} 个词包共用。重新构图后会同步替换，继续吗？`
          : "重新构图会替换当前已保存图片，继续吗？";
        if (!window.confirm(message)) break;
      }
      ensureSceneImage(state.wordIndex, index, true);
      break;
    }
    case "regenerate-entry":
      regenerateCurrentEntry();
      break;
    case "retry-image": {
      const word = currentWord();
      const index = state.sceneByWord[word.id] || 0;
      delete state.imageJobs[`${word.id}:${word.scenes[index].id}`];
      ensureSceneImage(state.wordIndex, index);
      break;
    }
    case "open-games":
      state.view = "games";
      state.guessReveal = false;
      state.multiReveal = false;
      render();
      break;
    case "play-guess":
      state.view = "guess";
      state.guessReveal = false;
      state.guessResult = "";
      render();
      requestAnimationFrame(() => document.querySelector("#guess-answer")?.focus());
      break;
    case "reveal-guess": {
      const value = document.querySelector("#guess-answer")?.value.trim().toLowerCase() || "";
      const target = state.activePack.words[state.guessIndex % state.activePack.words.length].word.toLowerCase();
      state.guessResult = value === target ? "答对了。" : value ? "你的感觉很接近；把它和这个词扣在一起：" : "先看答案，再回看画面的动作：";
      state.guessReveal = true;
      render();
      break;
    }
    case "next-guess":
      state.guessIndex = (state.guessIndex + 1) % state.activePack.words.length;
      state.guessReveal = false;
      state.guessResult = "";
      render();
      requestAnimationFrame(() => document.querySelector("#guess-answer")?.focus());
      break;
    case "play-multi":
      state.view = "multi";
      state.multiReveal = false;
      render();
      break;
    case "reveal-multi":
      state.multiReveal = true;
      render();
      break;
    case "restart-multi":
      state.multiReveal = false;
      render();
      break;
    default:
      break;
  }
});

app.addEventListener("input", (event) => {
  if (event.target.matches('[data-input="feeling"]')) {
    state.feelingAnswers[feelingKey(currentWord())] = event.target.value;
  }
});

app.addEventListener("submit", (event) => {
  if (event.target.id === "create-form") {
    event.preventDefault();
    createPack(event.target);
    return;
  }
  if (event.target.id === "access-form") {
    event.preventDefault();
    const key = String(new FormData(event.target).get("key") || "");
    state.accessError = "";
    state.busy = { title: "正在核对密钥", detail: "验证只在服务端完成。" };
    render();
    apiFetch("/api/access", {
      method: "POST",
      body: JSON.stringify({ key }),
    })
      .then(async () => {
        state.access.authenticated = true;
        state.busy = null;
        await Promise.all([loadPacks(), apiFetch("/api/demo").then((demo) => {
          state.demo = { ...demo, source: "demo" };
        })]);
        render();
      })
      .catch((error) => {
        state.access.authenticated = false;
        state.busy = null;
        state.accessError = error.message;
        render();
        requestAnimationFrame(() => document.querySelector("#access-key")?.focus());
      });
  }
});

document.addEventListener("keydown", (event) => {
  const editing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
  if (event.key === "Escape") {
    if (state.modal) {
      state.modal = null;
      render();
    } else if (state.view !== "home") goHome();
    return;
  }
  if (state.view === "lesson" && !editing) {
    if (event.key === "ArrowRight") advanceLesson();
    if (event.key === "ArrowLeft") previousLesson();
  }
});

async function init() {
  try {
    const health = await waitForLibraryReady(await apiFetch("/api/health"));
    state.health = health;
    state.persistentLibrary = Boolean(health.persistentLibrary);
    state.legacyPacks = readSavedPacks();
    state.legacyImported = localStorage.getItem("sensory-vocab-imported-v1") === "1";
    if (state.persistentLibrary) {
      state.access = await apiFetch("/api/access");
      if (state.access.authenticated) {
        const [demo] = await Promise.all([
          apiFetch("/api/demo"),
          loadPacks(),
        ]);
        state.demo = { ...demo, source: "demo" };
      }
    } else {
      const demo = await apiFetch("/api/demo");
      state.demo = { ...demo, source: "demo" };
      state.savedPacks = state.legacyPacks;
    }
    render();
  } catch (error) {
    app.innerHTML = `<div class="boot-screen"><div class="boot-mark">!</div><h1 style="font:600 28px var(--serif);margin:0">实验室暂时没有连上生成服务</h1><p style="max-width:520px;text-align:center;line-height:1.8">${esc(error.message)}<br />请稍后刷新；如果在本机运行，请确认 <code>server.py</code> 已启动。</p></div>`;
  }
}

init();
