import {
  SILENCE_COMMA_THRESHOLD,
  SILENCE_PLACEHOLDER_THRESHOLD,
  buildSegmentTokens,
} from "./transcript-utils.js";
import { TimelineController } from "./timeline.js";

const state = {
  projects: [],
  currentProjectId: null,
  transcript: null,
  transcriptVersion: null,
  fullTranscript: null,
  fullTranscriptVersion: null,
  transcriptLoadMode: "paged",
  transcriptSizeBytes: null,
  selectionVersion: null,
  pageOffset: 0,
  pageLimit: 250,
  tokens: [],
  tokenMap: new Map(),
  wordNodeMap: new Map(),
  selectedKeys: new Set(),
  deletedKeys: new Set(),
  deleteRanges: [],
  hideDeleted: true,
  history: [],
  future: [],
  tokenFlashTimers: new Map(),
  currentTaskId: null,
  currentTaskType: null,
  taskPollingTimer: null,
  currentMediaName: null,
  currentMediaPath: null,
  projectMetadata: null,
  timelineController: null,
  timelineData: null,
  timelineDataVersion: null,
  timelineZoom: 1,
  mediaDuration: null,
  waveformTaskId: 0,
  timelineWaveform: null,
  waveformCache: new Map(),
  waveformSourceKey: null,
  localPreviewUrl: null,
  silenceCandidates: [],
  silencePlaybackStart: null,
  silencePlaybackEnd: null,
  searchQuery: "",
  searchMatches: [],
  searchPointer: 0,
  searchMatchSet: new Set(),
  searchPreviewKeys: new Set(),
  searchShouldScroll: false,
  selectionAnchor: null,
  cachedFillerQuery: "",
  cachedFillerMatches: [],
  commonFillerWords: [],
  projectFiles: [],
  currentProjectFileId: null,
  currentProjectFileRevision: 0,
  recentProjectFiles: [],
  openPanelProjectId: null,
  openPanelFiles: [],
  selectionSnapshots: [],
  selectionSnapshotLoading: false,
  selectionSnapshotError: null,
  selectionSnapshotDialogOpen: false,
  // BEGIN-EDIT
  timelineLastScrubTime: 0,
  timelineLastInteractionAt: 0,
  timelinePendingRange: null,
  timelinePendingRangeTimestamp: 0,
  segmentPlaybackEnd: null,
  timelineActivePreviewKeys: new Set(),
  timelineSelectionScheduled: false,
  transcribeButtonDefaultLabel: "",
  cutButtonDefaultLabel: "",
  transcriptHoverKey: null,
  importGuardInitialized: false,
  // END-EDIT
};

if (typeof window !== "undefined") {
  window.__subtitleState = state;
}
const dom = {};

const RECENT_FILES_STORAGE_KEY = "subtitleCutRecentFiles";
const MAX_RECENT_FILES = 8;
const ENGINE_MODEL_OPTIONS = {
  // BEGIN-EDIT
  paraformer: [
    { value: "paraformer-zh", label: "paraformer-zh" },
    { value: "paraformer-en", label: "paraformer-en" },
  ],
  // END-EDIT
};
const POLL_INTERVAL_MS = 2000;
const MAX_HISTORY = 100;
const DELETE_EPSILON = 0.0005;
const DISPLAY_TRANSCRIPT_SIZE_THRESHOLD = Infinity;

document.addEventListener("DOMContentLoaded", () => {
  loadCommonFillers();
  cacheDom();
  loadRecentProjectFilesFromStorage();
  initializeTimeline();
  bindEvents();
  refreshProjects();
  renderRecentFiles();
});

function cacheDom() {
  dom.createForm = document.querySelector("#create-project-form");
  dom.createFileInput = document.querySelector("#transcript-file");
  dom.createNameInput = document.querySelector("#project-name");
  dom.uploadForm = document.querySelector("#upload-form");
  dom.mediaFileInput = document.querySelector("#media-file");
  dom.transcribeSubmitButton = dom.uploadForm?.querySelector("button[type='submit']");
  dom.engineSelect = document.querySelector("#engine-select");
  dom.modelSelect = document.querySelector("#model-select");
  dom.deviceSelect = document.querySelector("#device-select");
  dom.fileOpenBtn = document.querySelector("#file-open");
  dom.fileSaveBtn = document.querySelector("#file-save");
  dom.fileSaveAsBtn = document.querySelector("#file-save-as");
  dom.fileCloseBtn = document.querySelector("#file-close");
  dom.menuOpenOptions = document.querySelector("#menu-open-options");
  dom.recentFileList = document.querySelector("#recent-project-files");
  dom.fileOpenBackdrop = document.querySelector("#file-open-backdrop");
  dom.fileOpenPanel = document.querySelector("#file-open-panel");
  dom.fileOpenClose = document.querySelector("#file-open-close");
  dom.fileOpenRefresh = document.querySelector("#file-open-refresh");
  dom.openProjectList = document.querySelector("#open-project-list");
  dom.openProjectFileList = document.querySelector("#open-project-file-list");
  dom.openProjectActiveName = document.querySelector("#open-project-active-name");
  // BEGIN-EDIT
  if (dom.engineSelect) {
    const engineField = dom.engineSelect.closest(".operation-strip__field");
    if (engineField) {
      engineField.remove();
    } else {
      dom.engineSelect.remove();
    }
    dom.engineSelect = null;
  }
  if (!state.importGuardInitialized && typeof handleMediaSelection === "function") {
    handleMediaSelection = function guardedHandleMediaSelection(event) {
      try {
        const inputElement = event?.target;
        const file = inputElement?.files?.[0];
        if (!file) {
          if (inputElement) {
            inputElement.value = "";
          }
          logInfo("已取消导入新的音视频文件。");
          return;
        }

        const activeTaskId = state.currentTaskId;
        const activeTaskType = state.currentTaskType || "";
        if (activeTaskId) {
          const taskLabel = activeTaskType === "cut" ? "生成剪辑" : "转写";
          const confirmAbort = window.confirm(`当前正在${taskLabel}，是否中止后导入新的音视频？`);
          if (!confirmAbort) {
            logInfo("已取消导入新的音视频文件。");
            if (inputElement) {
              inputElement.value = "";
            }
            return;
          }
          stopTaskPolling();
          state.currentTaskId = null;
          state.currentTaskType = null;
          if (activeTaskType === "transcribe" && dom.taskStatus) {
            dom.taskStatus.textContent = "已中止当前转写任务。";
            resetTranscribeButton();
          } else if (activeTaskType === "cut" && dom.cutStatus) {
            dom.cutStatus.textContent = "已中止当前生成剪辑任务。";
            resetCutButton();
          }
          logWarn(`已中止当前${taskLabel}任务，准备导入新的音视频。`);
        }

        const hasTranscriptData = Boolean(state.transcript || state.fullTranscript);
        const hasTokens = Boolean(state.tokens && state.tokens.length);
        const hasMedia = Boolean(state.currentMediaPath || state.localPreviewUrl);
        const hasWorkspaceContent =
          hasTranscriptData ||
          hasTokens ||
          hasMedia ||
          Boolean(state.currentProjectId) ||
          Boolean(state.projectMetadata);

        if (hasWorkspaceContent) {
          const confirmMessage = hasTranscriptData
            ? "当前场景已生成文稿，继续导入将关闭当前项目并创建新项目。是否继续？"
            : "当前已打开音视频，继续导入将关闭之前的内容。是否继续？";
          const confirmReset = window.confirm(confirmMessage);
          if (!confirmReset) {
            logInfo("已取消导入新的音视频文件。");
            if (inputElement) {
              inputElement.value = "";
            }
            return;
          }
          resetWorkspaceState({ keepLocalPreview: false });
        }

        state.currentMediaName = file.name;
        if (state.localPreviewUrl) {
          URL.revokeObjectURL(state.localPreviewUrl);
        }
        state.localPreviewUrl = URL.createObjectURL(file);
        updateMediaSource({ previewOnly: true });
        refreshTimelineWaveform();
        logInfo(`已选择音视频文件：${state.currentMediaName}`);
      } catch (error) {
        const message = error instanceof Error ? error : new Error(String(error));
        logError(message);
      }
    };
    state.importGuardInitialized = true;
  }
  // END-EDIT
  dom.taskStatus = document.querySelector("#task-status");
  dom.cutForm = document.querySelector("#cut-form");
  dom.cutSubmitButton = dom.cutForm?.querySelector("button[type='submit']");
  dom.cutOutputName = document.querySelector("#cut-output-name");
  dom.cutReencode = document.querySelector("#cut-reencode");
  dom.cutSnapZero = document.querySelector("#cut-snap-zero");
  dom.cutXfadeMs = document.querySelector("#cut-xfade-ms");
  dom.cutChunkSize = document.querySelector("#cut-chunk-size");
  dom.exportSrtButton = document.querySelector("#export-srt-button");
  dom.cutStatus = document.querySelector("#cut-status");
  if (dom.cutSubmitButton) {
    const label =
      dom.cutSubmitButton.textContent?.trim() || state.cutButtonDefaultLabel || "生成剪辑";
    if (dom.cutSubmitButton.dataset) {
      dom.cutSubmitButton.dataset.defaultLabel = label;
    }
    state.cutButtonDefaultLabel = label;
  }
  dom.optionsPanel = document.querySelector("#options-panel");
  dom.optionsClose = document.querySelector("#options-close");
  dom.optionsBackdrop = document.querySelector("#options-backdrop");
  dom.workspace = document.querySelector("#workspace");
  dom.prevPageBtn = document.querySelector("#prev-page");
  dom.nextPageBtn = document.querySelector("#next-page");
  dom.pageInfo = document.querySelector("#page-info");
  dom.segmentsContainer = document.querySelector("#segments");
  dom.log = document.querySelector("#activity-log");
  dom.menuToggleLog = document.querySelector("#menu-toggle-log");
  dom.logDrawer = document.querySelector("#log-drawer");
  dom.logClose = document.querySelector("#log-close");
  dom.fillerWordsInput = document.querySelector("#filler-words");
  dom.pageSizeInput = document.querySelector("#page-size");
  dom.analyzeSilenceBtn = document.querySelector("#analyze-silence");
  dom.silenceStatus = document.querySelector("#silence-status");
  dom.silenceList = document.querySelector("#silence-list");
  dom.mediaPlayer = document.querySelector("#media-player");
  dom.previewFit = document.querySelector("#preview-fit");
  dom.previewOriginal = document.querySelector("#preview-original");
  dom.timelineZoomOut = document.querySelector("#timeline-zoom-out");
  dom.timelineZoomIn = document.querySelector("#timeline-zoom-in");
  dom.timelineFit = document.querySelector("#timeline-fit");
  dom.timelineTrack = document.querySelector("#timeline-track");
  dom.manuscriptDeleteSilence = document.querySelector("#manuscript-delete-silence");
  dom.manuscriptDeleteFiller = document.querySelector("#manuscript-delete-filler");
  dom.fillerSettings = document.querySelector("#filler-settings");
  dom.searchInput = document.querySelector("#transcript-search");
  dom.searchActions = document.querySelector("#search-actions");
  dom.searchSkip = document.querySelector("#search-skip");
  dom.searchDeleteOne = document.querySelector("#search-delete-one");
  dom.searchDeleteAll = document.querySelector("#search-delete-all");
  dom.menuEditCommonFillers = document.querySelector("#menu-edit-common-fillers");
  dom.commonFillersPanel = document.querySelector("#common-fillers-panel");
  dom.commonFillersBackdrop = document.querySelector("#common-fillers-backdrop");
  dom.commonFillersClose = document.querySelector("#common-fillers-close");
  dom.commonFillersSave = document.querySelector("#common-fillers-save");
  dom.commonFillersInput = document.querySelector("#common-fillers-input");
  dom.snapshotPanel = document.querySelector("#snapshot-panel");
  dom.snapshotBackdrop = document.querySelector("#snapshot-backdrop");
  dom.snapshotList = document.querySelector("#snapshot-list");
  dom.snapshotEmpty = document.querySelector("#snapshot-empty");
  dom.snapshotClose = document.querySelector("#snapshot-close");
  dom.snapshotCreate = document.querySelector("#snapshot-create");
  dom.showDeletedToggle = document.querySelector("#toggle-show-deleted");
  dom.previewSilenceBtn = document.querySelector("#preview-silence");
  dom.menuBar = document.querySelector("#menu-bar");
  dom.menus = Array.from(document.querySelectorAll(".menu"));
  if (dom.pageInfo) {
    dom.pageInfo.textContent = "未选择项目";
  }
  if (dom.taskStatus) {
    dom.taskStatus.textContent = "";
  }
  if (dom.cutStatus) {
    dom.cutStatus.textContent = "";
  }
  updateSilenceActionState();
}

function initializeTimeline() {
  if (!dom.timelineTrack) return;
  state.timelineController = new TimelineController({
    container: dom.timelineTrack,
    onScrub: handleTimelineScrub,
    onToggleRange: handleTimelineToggle,
    onHoverKeys: (keys, active) => highlightPreviewKeys(keys, active),
    onZoomChange: handleTimelineZoomChange,
    isRangeSelected: (range) => isRangeSelected(range.start, range.end),
    getAnchor: () => state.selectionAnchor,
    onFocusRange: handleTimelineFocusRange,
  });
  if (dom.mediaPlayer) {
    state.timelineController.bindMedia(dom.mediaPlayer);
  }
  refreshTimelineWaveform();
}

function bindEvents() {
  if (dom.createForm) {
    dom.createForm.addEventListener("submit", handleCreateProject);
  }
  if (dom.createFileInput) {
    dom.createFileInput.addEventListener("change", handleTranscriptFilePick);
  }
  if (dom.uploadForm) {
    dom.uploadForm.addEventListener("submit", handleUploadAndTranscribe);
  }
  if (dom.mediaFileInput) {
    dom.mediaFileInput.addEventListener("change", handleMediaSelection);
  }
  if (dom.engineSelect) {
    dom.engineSelect.addEventListener("change", handleEngineChange);
  }
  if (dom.cutForm) {
    dom.cutForm.addEventListener("submit", handleCutSubmit);
  }
  if (dom.exportSrtButton) {
    dom.exportSrtButton.addEventListener("click", handleExportSrtOnly);
  }
  if (dom.prevPageBtn) {
    dom.prevPageBtn.addEventListener("click", () => changePage(-1));
  }
  if (dom.nextPageBtn) {
    dom.nextPageBtn.addEventListener("click", () => changePage(1));
  }
  if (dom.pageSizeInput) {
    dom.pageSizeInput.addEventListener("change", handlePageSizeChange);
  }
  if (dom.segmentsContainer) {
    dom.segmentsContainer.addEventListener("click", handleTranscriptClick);
    dom.segmentsContainer.addEventListener("dblclick", handleTranscriptDoubleClick);
    dom.segmentsContainer.addEventListener("keydown", handleTranscriptKeydown);
    dom.segmentsContainer.addEventListener("mouseup", handleTranscriptMouseUp);
    dom.segmentsContainer.addEventListener("pointermove", handleTranscriptPointerMove);
    dom.segmentsContainer.addEventListener("pointerleave", handleTranscriptPointerLeave);
    dom.segmentsContainer.addEventListener("pointerdown", handleTranscriptPointerDown);
    dom.segmentsContainer.addEventListener("pointercancel", handleTranscriptPointerLeave);
    dom.segmentsContainer.addEventListener("focus", () => dom.segmentsContainer.classList.add("focused"));
    dom.segmentsContainer.addEventListener("blur", () => dom.segmentsContainer.classList.remove("focused"));
  }
  if (dom.fileOpenBtn) {
    dom.fileOpenBtn.addEventListener("click", () => openFileOpenPanel());
  }
  if (dom.fileSaveBtn) {
    dom.fileSaveBtn.addEventListener("click", () => handleFileSave());
  }
  if (dom.fileSaveAsBtn) {
    dom.fileSaveAsBtn.addEventListener("click", () => handleFileSaveAs());
  }
  if (dom.fileCloseBtn) {
    dom.fileCloseBtn.addEventListener("click", () => handleFileClose());
  }
  if (dom.fileOpenClose) {
    dom.fileOpenClose.addEventListener("click", () => closeFileOpenPanel());
  }
  if (dom.fileOpenBackdrop) {
    dom.fileOpenBackdrop.addEventListener("click", () => closeFileOpenPanel());
  }
  if (dom.fileOpenRefresh) {
    dom.fileOpenRefresh.addEventListener("click", async () => {
      await refreshProjects();
      if (Number.isInteger(state.openPanelProjectId)) {
        await selectProjectInOpenPanel(state.openPanelProjectId);
      } else if (state.projects.length) {
        await selectProjectInOpenPanel(state.projects[0].id);
      } else {
        renderOpenProjectFiles();
      }
    });
  }
  if (dom.menuToggleLog) {
    dom.menuToggleLog.addEventListener("click", () => toggleLogDrawer());
  }
  if (dom.logClose) {
    dom.logClose.addEventListener("click", () => toggleLogDrawer(false));
  }
  if (dom.fillerWordsInput) {
    dom.fillerWordsInput.addEventListener("input", handleFillerInput);
  }
  if (dom.manuscriptDeleteFiller) {
    dom.manuscriptDeleteFiller.addEventListener("mouseenter", () => highlightFillerPreview(true));
    dom.manuscriptDeleteFiller.addEventListener("mouseleave", () => highlightFillerPreview(false));
    dom.manuscriptDeleteFiller.addEventListener("click", applyFillerWordsAndSave);
  }
  if (dom.manuscriptDeleteSilence) {
    dom.manuscriptDeleteSilence.addEventListener("mouseenter", () => highlightSilencePlaceholders(true));
    dom.manuscriptDeleteSilence.addEventListener("mouseleave", () => highlightSilencePlaceholders(false));
    dom.manuscriptDeleteSilence.addEventListener("click", handleDeleteAllSilencePlaceholders);
  }
  if (dom.searchInput) {
    dom.searchInput.addEventListener("focus", () => dom.searchActions?.classList.add("visible"));
    dom.searchInput.addEventListener("blur", () => {
      if (!state.searchQuery) {
        dom.searchActions?.classList.remove("visible");
      }
    });
    dom.searchInput.addEventListener("input", handleSearchInput);
    dom.searchInput.addEventListener("keydown", handleSearchKeydown);
  }
  if (dom.searchDeleteOne) {
    dom.searchDeleteOne.addEventListener("click", () => deleteSearchMatches(false));
    dom.searchDeleteOne.addEventListener("mouseenter", () => highlightSearchPreview(true, false));
    dom.searchDeleteOne.addEventListener("mouseleave", () => highlightSearchPreview(false));
  }
  if (dom.searchSkip) {
    dom.searchSkip.addEventListener("click", handleSearchSkip);
  }
  if (dom.searchDeleteAll) {
    dom.searchDeleteAll.addEventListener("click", () => deleteSearchMatches(true));
    dom.searchDeleteAll.addEventListener("mouseenter", () => highlightSearchPreview(true, true));
    dom.searchDeleteAll.addEventListener("mouseleave", () => highlightSearchPreview(false));
  }
  if (dom.showDeletedToggle) {
    dom.showDeletedToggle.addEventListener("change", handleToggleShowDeleted);
  }
  if (dom.previewFit) {
    dom.previewFit.addEventListener("click", () => setPreviewFit("contain"));
  }
  if (dom.previewOriginal) {
    dom.previewOriginal.addEventListener("click", () => setPreviewFit("cover"));
  }
  if (dom.timelineZoomOut) {
    dom.timelineZoomOut.addEventListener("click", () => adjustTimelineZoom(-0.25));
  }
  if (dom.timelineZoomIn) {
    dom.timelineZoomIn.addEventListener("click", () => adjustTimelineZoom(0.25));
  }
  if (dom.timelineFit) {
    dom.timelineFit.addEventListener("click", resetTimelineZoom);
  }
  if (dom.mediaPlayer) {
    dom.mediaPlayer.addEventListener("timeupdate", handleMediaTimeUpdate);
  }
  if (dom.menuOpenOptions) {
    dom.menuOpenOptions.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      closeAllMenus();
      openOptionsPanel();
    });
  }
  if (dom.optionsClose) {
    dom.optionsClose.addEventListener("click", () => closeOptionsPanel());
  }
  if (dom.optionsBackdrop) {
    dom.optionsBackdrop.addEventListener("click", () => closeOptionsPanel());
  }
  if (dom.menuEditCommonFillers) {
    dom.menuEditCommonFillers.addEventListener("click", () => {
      closeAllMenus();
      openCommonFillersPanel();
    });
  }
  if (dom.fillerSettings) {
    dom.fillerSettings.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      openCommonFillersPanel();
    });
  }
  if (dom.commonFillersClose) {
    dom.commonFillersClose.addEventListener("click", () => closeCommonFillersPanel());
  }
  if (dom.commonFillersBackdrop) {
    dom.commonFillersBackdrop.addEventListener("click", () => closeCommonFillersPanel());
  }
  if (dom.commonFillersSave) {
    dom.commonFillersSave.addEventListener("click", saveCommonFillers);
  }
  if (dom.snapshotClose) {
    dom.snapshotClose.addEventListener("click", () => closeSnapshotPanel());
  }
  if (dom.snapshotBackdrop) {
    dom.snapshotBackdrop.addEventListener("click", () => closeSnapshotPanel());
  }
  if (dom.snapshotCreate) {
    dom.snapshotCreate.addEventListener("click", () => handleSnapshotCreate());
  }
  if (dom.snapshotList) {
    dom.snapshotList.addEventListener("click", handleSnapshotAction);
  }
  dom.menus?.forEach((menu) => {
    const trigger = menu.querySelector(".menu-trigger");
    if (!trigger) return;
    trigger.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      toggleMenu(menu);
    });
    menu.addEventListener("click", (event) => event.stopPropagation());
  });
  document.addEventListener("click", handleDocumentClick);
  document.addEventListener("keydown", handleKeydown);
  handleEngineChange();
}

function toggleMenu(menu) {
  if (!menu) return;
  const alreadyOpen = menu.classList.contains("open");
  closeAllMenus();
  if (!alreadyOpen) {
    menu.classList.add("open");
  }
}

function closeAllMenus() {
  dom.menus?.forEach((item) => item.classList.remove("open"));
}

function handleDocumentClick(event) {
  const optionsOpen = dom.optionsPanel?.classList.contains("open");
  const clickedInsideOptions = optionsOpen && dom.optionsPanel.contains(event.target);
  if (optionsOpen && !clickedInsideOptions) {
    closeOptionsPanel();
  }
  const commonOpen = dom.commonFillersPanel?.classList.contains("open");
  const clickedInsideCommon = commonOpen && dom.commonFillersPanel.contains(event.target);
  if (commonOpen && !clickedInsideCommon && dom.commonFillersBackdrop !== event.target) {
    closeCommonFillersPanel();
  }
  if (dom.menuBar && dom.menuBar.contains(event.target)) {
    return;
  }
  closeAllMenus();
  if (dom.logDrawer && !dom.logDrawer.contains(event.target)) {
    toggleLogDrawer(false);
  }
}

function openOptionsPanel() {
  if (!dom.optionsPanel) return;
  dom.optionsPanel.classList.add("open");
  dom.optionsPanel.setAttribute("aria-hidden", "false");
  if (dom.optionsBackdrop) {
    dom.optionsBackdrop.classList.add("open");
    dom.optionsBackdrop.setAttribute("aria-hidden", "false");
  }
  const firstField = dom.optionsPanel.querySelector("select, input");
  if (firstField) {
    firstField.focus();
  }
}

function closeOptionsPanel() {
  if (!dom.optionsPanel || !dom.optionsPanel.classList.contains("open")) {
    return false;
  }
  dom.optionsPanel.classList.remove("open");
  dom.optionsPanel.setAttribute("aria-hidden", "true");
  if (dom.optionsBackdrop) {
    dom.optionsBackdrop.classList.remove("open");
    dom.optionsBackdrop.setAttribute("aria-hidden", "true");
  }
  return true;
}

function openCommonFillersPanel() {
  if (!dom.commonFillersPanel) return;
  const initialValue = state.commonFillerWords.join(" ");
  if (dom.commonFillersInput) {
    dom.commonFillersInput.value = initialValue;
    setTimeout(() => dom.commonFillersInput?.focus(), 0);
  }
  dom.commonFillersPanel.classList.add("open");
  dom.commonFillersPanel.setAttribute("aria-hidden", "false");
  if (dom.commonFillersBackdrop) {
    dom.commonFillersBackdrop.classList.add("open");
    dom.commonFillersBackdrop.setAttribute("aria-hidden", "false");
  }
}

function closeCommonFillersPanel() {
  if (!dom.commonFillersPanel || !dom.commonFillersPanel.classList.contains("open")) {
    return false;
  }
  dom.commonFillersPanel.classList.remove("open");
  dom.commonFillersPanel.setAttribute("aria-hidden", "true");
  if (dom.commonFillersBackdrop) {
    dom.commonFillersBackdrop.classList.remove("open");
    dom.commonFillersBackdrop.setAttribute("aria-hidden", "true");
  }
  return true;
}

function saveCommonFillersLegacy() {
  saveCommonFillers();
}

function loadCommonFillersLegacy() {
  loadCommonFillers();
}

async function saveCommonFillers(event) {
  if (event) {
    event.preventDefault();
  }
  if (!dom.commonFillersInput) {
    closeCommonFillersPanel();
    return;
  }
  const raw = dom.commonFillersInput.value || "";
  const tokens = parseRawFillerList(raw);
  const payload = { words: tokens };

  try {
    if (dom.commonFillersSave) {
      dom.commonFillersSave.disabled = true;
    }
    const response = await fetch("/api/common-fillers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      let message = `保存常用水词失败 (${response.status})`;
      try {
        const data = await response.json();
        if (data?.error) {
          message = data.error;
        }
      } catch (error) {
        // ignore json 解析失败
      }
      throw new Error(message);
    }
    const data = await response.json();
    const savedWords = Array.isArray(data?.words) ? data.words : tokens;
    state.commonFillerWords = savedWords
      .map((token) => String(token || "").trim())
      .filter(Boolean);
    state.cachedFillerQuery = "";
    state.cachedFillerMatches = [];
    updateSilenceActionState();
    highlightPreviewKeys([], false);
    closeCommonFillersPanel();
    const count = state.commonFillerWords.length;
    logInfo(count ? `常用水词已更新，共 ${count} 条。` : "常用水词列表已清空。");
  } catch (error) {
    logError(error instanceof Error ? error : new Error(String(error)));
  } finally {
    if (dom.commonFillersSave) {
      dom.commonFillersSave.disabled = false;
    }
  }
}

async function loadCommonFillers() {
  try {
    const response = await fetch("/api/common-fillers");
    if (!response.ok) {
      throw new Error(`读取常用水词失败 (${response.status})`);
    }
    const data = await response.json();
    const words = Array.isArray(data?.words) ? data.words : [];
    state.commonFillerWords = words
      .map((token) => String(token || "").trim())
      .filter(Boolean);
  } catch (error) {
    console.warn("读取常用水词失败", error);
    state.commonFillerWords = [];
    logError(error instanceof Error ? error : new Error(String(error)));
  } finally {
    state.cachedFillerQuery = "";
    state.cachedFillerMatches = [];
  }
}

function setPreviewFit(mode) {
  if (!dom.mediaPlayer) return;
  if (mode === "contain") {
    dom.mediaPlayer.style.objectFit = "contain";
    logInfo("预览画面调整为适应窗口");
  } else {
    dom.mediaPlayer.style.objectFit = "";
    logInfo("预览画面恢复原始比例");
  }
}

function adjustTimelineZoom(delta) {
  if (!state.timelineController) return;
  const currentZoom = state.timelineController.getZoom() || state.timelineZoom || 1;
  const next = Number(currentZoom) + Number(delta || 0);
  const applied = state.timelineController.setZoom(next);
  if (Number.isFinite(applied)) {
    state.timelineZoom = applied;
  }
}

function resetTimelineZoom() {
  if (!state.timelineController) return;
  state.timelineController.autoFit();
  state.timelineZoom = state.timelineController.getZoom() || 1;
  logInfo("时间轴已自适应显示");
}

// ---------------------------------------------------------------------------
// 数据加载与项目管理
// ---------------------------------------------------------------------------

async function refreshProjects() {
  try {
    const response = await fetch("/api/projects");
    if (!response.ok) throw new Error(`加载项目列表失败: ${response.status}`);
    const data = await response.json();
    state.projects = Array.isArray(data.projects) ? data.projects : [];
    const currentExists =
      state.currentProjectId != null && state.projects.some((project) => project.id === state.currentProjectId);
    if (state.currentProjectId && !currentExists) {
      resetWorkspaceState();
    }
    renderProjects();
  } catch (error) {
    logError(error);
    if (dom.cutStatus) {
      const message = error instanceof Error ? error.message : String(error);
      dom.cutStatus.textContent = message;
    }
    resetCutButton();
  }
}

function renderProjects() {
  if (!dom.openProjectList) return;
  dom.openProjectList.innerHTML = "";
  if (!state.projects.length) {
    const placeholder = document.createElement("li");
    placeholder.className = "open-project-item";
    placeholder.textContent = "暂无项目";
    dom.openProjectList.appendChild(placeholder);
    return;
  }
  state.projects.forEach((project) => {
    const item = document.createElement("li");
    item.className = "open-project-item";
    if (state.openPanelProjectId === project.id) {
      item.classList.add("active");
    }
    const nameSpan = document.createElement("span");
    nameSpan.className = "open-project-item__name";
    nameSpan.textContent = project.name || `项目 #${project.id}`;
    const metaSpan = document.createElement("span");
    metaSpan.className = "open-project-item__meta";
    metaSpan.textContent = `#${project.id}`;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "ghost-button open-project-item__delete";
    removeBtn.textContent = "删除";
    removeBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteProject(project.id);
    });
    item.appendChild(nameSpan);
    item.appendChild(metaSpan);
    item.appendChild(removeBtn);
    item.addEventListener("click", () => selectProjectInOpenPanel(project.id));
    dom.openProjectList.appendChild(item);
  });
}

function loadRecentProjectFilesFromStorage() {
  try {
    const stored = window.localStorage?.getItem(RECENT_FILES_STORAGE_KEY);
    if (!stored) {
      state.recentProjectFiles = [];
      return;
    }
    const parsed = JSON.parse(stored);
    if (!Array.isArray(parsed)) {
      state.recentProjectFiles = [];
      return;
    }
    state.recentProjectFiles = parsed
      .map((entry) => ({
        projectId: Number(entry.projectId),
        fileId: Number(entry.fileId),
        projectName: typeof entry.projectName === "string" ? entry.projectName : "",
        fileName: typeof entry.fileName === "string" ? entry.fileName : "",
        openedAt: Number(entry.openedAt) || Date.now(),
      }))
      .filter((entry) => Number.isInteger(entry.projectId) && Number.isInteger(entry.fileId));
  } catch (error) {
    console.warn("读取最近文件失败", error);
    state.recentProjectFiles = [];
  }
}

function saveRecentProjectFilesToStorage() {
  try {
    window.localStorage?.setItem(RECENT_FILES_STORAGE_KEY, JSON.stringify(state.recentProjectFiles));
  } catch (error) {
    console.warn("保存最近文件失败", error);
  }
}

function renderRecentFiles() {
  if (!dom.recentFileList) return;
  dom.recentFileList.innerHTML = "";
  if (!state.recentProjectFiles.length) {
    const placeholder = document.createElement("li");
    placeholder.className = "recent-file-item";
    placeholder.textContent = "暂无记录";
    dom.recentFileList.appendChild(placeholder);
    return;
  }
  state.recentProjectFiles.forEach((entry) => {
    const item = document.createElement("li");
    item.className = "recent-file-item";

    const nameSpan = document.createElement("span");
    nameSpan.className = "recent-file-item__name";
    const projectTitle = entry.projectName || `项目 #${entry.projectId}`;
    const fileTitle = entry.fileName || `工程文件 #${entry.fileId}`;
    nameSpan.textContent = `${projectTitle} — ${fileTitle}`;

    const metaSpan = document.createElement("span");
    metaSpan.className = "recent-file-item__meta";
    const openedDate = new Date(entry.openedAt);
    metaSpan.textContent = Number.isNaN(openedDate.valueOf()) ? "" : openedDate.toLocaleString();

    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.className = "ghost-button recent-file-item__remove";
    removeBtn.textContent = "移除";
    removeBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      removeRecentProjectFile(entry.projectId, entry.fileId);
    });

    item.appendChild(nameSpan);
    item.appendChild(metaSpan);
    item.appendChild(removeBtn);
    item.addEventListener("click", () => handleRecentFileEntry(entry));
    dom.recentFileList.appendChild(item);
  });
}

function recordRecentProjectFile({ projectId, projectName, fileId, fileName }) {
  if (!Number.isInteger(projectId) || !Number.isInteger(fileId)) return;
  const timestamp = Date.now();
  const filtered = state.recentProjectFiles.filter(
    (item) => !(item.projectId === projectId && item.fileId === fileId),
  );
  filtered.unshift({
    projectId,
    projectName: projectName || `项目 #${projectId}`,
    fileId,
    fileName: fileName || `工程文件 #${fileId}`,
    openedAt: timestamp,
  });
  state.recentProjectFiles = filtered.slice(0, MAX_RECENT_FILES);
  saveRecentProjectFilesToStorage();
  renderRecentFiles();
}

function removeRecentProjectFile(projectId, fileId) {
  state.recentProjectFiles = state.recentProjectFiles.filter(
    (item) => !(item.projectId === projectId && item.fileId === fileId),
  );
  saveRecentProjectFilesToStorage();
  renderRecentFiles();
}

async function handleRecentFileEntry(entry) {
  if (!entry || !Number.isInteger(entry.projectId)) return;
  closeAllMenus();
  closeFileOpenPanel();
  try {
    await setCurrentProject(entry.projectId);
    if (Number.isInteger(entry.fileId)) {
      await loadProjectFile(entry.fileId);
    }
  } catch (error) {
    logError(error);
    resetTranscribeButton();
  }
}

async function openFileOpenPanel() {
  closeAllMenus();
  if (!dom.fileOpenPanel) return;
  dom.fileOpenPanel.classList.add("open");
  dom.fileOpenPanel.setAttribute("aria-hidden", "false");
  dom.fileOpenBackdrop?.classList.add("open");
  dom.fileOpenBackdrop?.setAttribute("aria-hidden", "false");
  await refreshProjects();
  const availableProjects = state.projects || [];
  if (!availableProjects.length) {
    state.openPanelProjectId = null;
    state.openPanelFiles = [];
    if (dom.openProjectActiveName) dom.openProjectActiveName.textContent = "";
    renderProjects();
    renderOpenProjectFiles();
    return;
  }
  const preferredId = availableProjects.some((item) => item.id === state.currentProjectId)
    ? state.currentProjectId
    : availableProjects[0].id;
  await selectProjectInOpenPanel(preferredId);
}

function closeFileOpenPanel() {
  const panelOpen = dom.fileOpenPanel?.classList.contains("open");
  if (!panelOpen) {
    state.openPanelProjectId = null;
    state.openPanelFiles = [];
    if (dom.openProjectActiveName) {
      dom.openProjectActiveName.textContent = "";
    }
    return false;
  }
  dom.fileOpenPanel.classList.remove("open");
  dom.fileOpenPanel.setAttribute("aria-hidden", "true");
  dom.fileOpenBackdrop?.classList.remove("open");
  dom.fileOpenBackdrop?.setAttribute("aria-hidden", "true");
  state.openPanelProjectId = null;
  state.openPanelFiles = [];
  if (dom.openProjectActiveName) {
    dom.openProjectActiveName.textContent = "";
  }
  renderProjects();
  renderOpenProjectFiles();
  return true;
}


async function selectProjectInOpenPanel(projectId) {
  if (!Number.isInteger(projectId)) return;
  state.openPanelProjectId = projectId;
  renderProjects();
  const project = state.projects.find((item) => item.id === projectId);
  if (dom.openProjectActiveName) {
    dom.openProjectActiveName.textContent = project ? project.name || `项目 #${project.id}` : "";
  }
  try {
    const response = await fetch(`/api/project-files?project_id=${projectId}`);
    if (!response.ok) throw new Error(`加载工程文件失败: ${response.status}`);
    const data = await response.json();
    state.openPanelFiles = Array.isArray(data?.files) ? data.files : [];
  } catch (error) {
    logError(error);
    state.openPanelFiles = [];
  }
  renderOpenProjectFiles();
}

function renderOpenProjectFiles() {
  if (!dom.openProjectFileList) return;
  dom.openProjectFileList.innerHTML = "";
  if (!state.openPanelFiles.length) {
    const placeholder = document.createElement("li");
    placeholder.className = "open-project-file-item";
    placeholder.textContent = state.openPanelProjectId ? "该项目暂无工程文件" : "请选择项目";
    dom.openProjectFileList.appendChild(placeholder);
    return;
  }
  state.openPanelFiles.forEach((file) => {
    const item = document.createElement("li");
    item.className = "open-project-file-item";
    if (file.id === state.currentProjectFileId && state.openPanelProjectId === state.currentProjectId) {
      item.classList.add("active");
    }
    const nameSpan = document.createElement("span");
    nameSpan.className = "open-project-file-item__name";
    nameSpan.textContent = file.name || `工程文件 #${file.id}`;
    const metaSpan = document.createElement("span");
    metaSpan.className = "open-project-file-item__meta";
    metaSpan.textContent = formatProjectFileTimestamp(file.updated_at || file.created_at);
    item.appendChild(nameSpan);
    item.appendChild(metaSpan);
    item.addEventListener("click", () => openProjectFileFromPanel(state.openPanelProjectId, file.id));
    dom.openProjectFileList.appendChild(item);
  });
}

async function openProjectFileFromPanel(projectId, fileId) {
  if (!Number.isInteger(projectId)) return;
  closeFileOpenPanel();
  try {
    await setCurrentProject(projectId);
    if (Number.isInteger(fileId)) {
      await loadProjectFile(fileId);
    }
  } catch (error) {
    logError(error);
  }
}

async function refreshProjectFiles(options = {}) {
  const projectId = state.currentProjectId;
  if (!Number.isInteger(projectId)) {
    state.projectFiles = [];
    state.currentProjectFileId = null;
    state.currentProjectFileRevision = 0;
    renderProjectFiles();
    renderProjects();
    renderOpenProjectFiles();
    return;
  }
  const { autoSelect = false } = options;
  try {
    const response = await fetch(`/api/project-files?project_id=${projectId}`);
    if (!response.ok) throw new Error(`加载工程文件列表失败: ${response.status}`);
    const data = await response.json();
    const files = Array.isArray(data?.files) ? data.files : [];
    const previousId = state.currentProjectFileId;
    state.projectFiles = files;
    renderProjectFiles();
    let targetId = previousId && files.some((file) => file.id === previousId) ? previousId : null;
    if (!targetId && autoSelect && files.length) {
      targetId = files[0].id;
    }
    if (targetId && (autoSelect || targetId !== previousId)) {
      await loadProjectFile(targetId, { silent: autoSelect });
    } else if (!targetId) {
      state.currentProjectFileId = null;
      state.currentProjectFileRevision = 0;
      renderProjectFiles();
    }
  } catch (error) {
    logError(error);
  }
}

function renderProjectFiles() {
  if (state.openPanelProjectId === state.currentProjectId) {
    state.openPanelFiles = Array.isArray(state.projectFiles) ? state.projectFiles.slice() : [];
    renderOpenProjectFiles();
  }
}
async function loadProjectFile(projectFileId, options = {}) {
  if (!Number.isInteger(projectFileId) || !state.currentProjectId) {
    return;
  }
  const { silent = false } = options;
  try {
    const response = await fetch(`/api/project-files/${projectFileId}`);
    if (!response.ok) throw new Error(`加载工程文件失败: ${response.status}`);
    const data = await response.json();
    const file = data?.file;
    if (!file) {
      throw new Error("工程文件返回数据异常");
    }
    state.currentProjectFileId = file.id;
    state.currentProjectFileRevision = Number(file.revision) || 0;
    const payload = file.payload || {};
    const deleteRanges = Array.isArray(payload.delete_ranges) ? payload.delete_ranges : [];
    applyDeleteRanges(deleteRanges, { silent: true });
    state.selectionVersion = null;
    updateDeletedVisuals();
    updateTimelineSelection();
    refreshTimelineWaveform();
    updateSilenceActionState();
    resetHistory("加载工程");
    renderProjectFiles();
    if (!silent) {
      const project = state.projects.find((item) => item.id === state.currentProjectId);
      const projectName = project?.name || `项目 #${state.currentProjectId}`;
      const fileLabel = file.name || metadata.label || `工程文件 #${file.id}`;
      logInfo(`已打开工程文件: ${fileLabel}`);
      recordRecentProjectFile({
        projectId: state.currentProjectId,
        projectName,
        fileId: file.id,
        fileName: fileLabel,
      });
    }
  } catch (error) {
    logError(error);
  }
}

async function handleFileSaveAs() {
  closeAllMenus();
  if (!Number.isInteger(state.currentProjectId)) {
    logError(new Error("暂无选中的项目"));
    return;
  }
  const defaultName = state.projectFiles.length ? `工程副本 ${state.projectFiles.length + 1}` : "新建工程文件";
  const nameInput = window.prompt("请输入工程文件名称：", defaultName);
  if (nameInput == null) {
    return;
  }
  const name = nameInput.trim();
  if (!name) {
    logWarn("工程文件名称不能为空");
    return;
  }
  const deleteRanges = buildDeleteRangesFromDeletedKeys();
  const selectionPayload = {
    delete_ranges: deleteRanges,
    metadata: {
      updated_at: new Date().toISOString(),
      base_version: state.selectionVersion,
      label: name,
    },
  };
  try {
    const response = await fetch("/api/project-files", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: state.currentProjectId,
        name,
        selection: selectionPayload,
      }),
    });
    if (!response.ok) throw new Error(`创建工程文件失败: ${response.status}`);
    const data = await response.json();
    const file = data?.file;
    logInfo(`已创建工程文件：${name}`);
    await refreshProjectFiles({ autoSelect: false });
    if (file?.id) {
      await loadProjectFile(file.id);
    }
  } catch (error) {
    logError(error);
  }
}

async function handleFileSave() {
  closeAllMenus();
  if (!Number.isInteger(state.currentProjectId) || !Number.isInteger(state.currentProjectFileId)) {
    logWarn("当前没有打开的工程文件");
    return;
  }
  try {
    if (dom.cutSubmitButton) {
      dom.cutSubmitButton.disabled = true;
    }
    setCutButtonProgress(0);
    if (dom.cutStatus) {
      dom.cutStatus.textContent = "正在提交剪辑任务";
    }
    const deleteRanges = buildDeleteRangesFromDeletedKeys();
    await persistSelection(deleteRanges);
    logInfo("工程文件已保存");
  } catch (error) {
    logError(error);
  }
}

function handleFileClose() {
  closeAllMenus();
  if (!Number.isInteger(state.currentProjectId)) {
    return;
  }
  resetWorkspaceState();
  renderProjects();
  renderOpenProjectFiles();
  renderRecentFiles();
  logInfo("项目已关闭");
}



function formatProjectFileTimestamp(value) {
  if (!value) return "未知时间";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) {
    return "未知时间";
  }
  return date.toLocaleString();
}

async function deleteProject(projectId) {
  if (!Number.isInteger(projectId)) return;
  const confirmed = window.confirm(`确定要删除项目 #${projectId} 吗？该操作不可恢复。`);
  if (!confirmed) return;
  try {
    const response = await fetch(`/api/projects/${projectId}`, { method: "DELETE" });
    if (!response.ok) throw new Error(`删除项目失败: ${response.status}`);
    logInfo(`项目 #${projectId} 已删除`);
    if (state.currentProjectId === projectId) {
      resetWorkspaceState();
    }
    if (state.openPanelProjectId === projectId) {
      state.openPanelProjectId = null;
      state.openPanelFiles = [];
      if (dom.openProjectActiveName) {
        dom.openProjectActiveName.textContent = "";
      }
    }
    state.recentProjectFiles = state.recentProjectFiles.filter((item) => item.projectId !== projectId);
    saveRecentProjectFilesToStorage();
    renderRecentFiles();
    await refreshProjects();
    if (dom.fileOpenPanel?.classList.contains("open")) {
      if (state.projects.length) {
        await selectProjectInOpenPanel(state.projects[0].id);
      } else {
        renderProjects();
        renderOpenProjectFiles();
      }
    }
  } catch (error) {
    logError(error);
  }
}


async function setCurrentProject(projectId) {
  if (!Number.isInteger(projectId)) return;
  // BEGIN-EDIT
  closeAllMenus();
  // END-EDIT
  if (state.currentProjectId === projectId && state.transcript) {
    return;
  }
  resetWorkspaceState({ keepLocalPreview: false });
  state.currentProjectId = projectId;
  state.pageOffset = 0;
  state.selectionAnchor = null;
  state.selectedKeys.clear();
  state.deletedKeys.clear();
  state.deleteRanges = [];
  state.history = [];
  state.future = [];
  renderProjects();
  await loadCurrentProject({ force: true });
}

async function loadCurrentProject(options = {}) {
  const projectId = state.currentProjectId;
  if (!Number.isInteger(projectId)) {
    return;
  }
  const { force = false } = options;
  try {
    const [transcriptResponse, metadataData] = await Promise.all([
      fetchTranscriptPreferred(projectId),
      fetchMetadata(projectId),
    ]);

    if (transcriptResponse?.data) {
      const transcriptPayload = transcriptResponse.data.transcript || null;
      const transcriptVersion = transcriptResponse.data.version ?? null;
      state.transcript = transcriptPayload;
      state.transcriptVersion = transcriptVersion;
      state.transcriptSizeBytes = transcriptResponse.sizeBytes ?? null;
      state.transcriptLoadMode = transcriptResponse.mode || "paged";
      if (state.transcriptLoadMode === "full" && transcriptPayload) {
        state.fullTranscript = transcriptPayload;
        state.fullTranscriptVersion = transcriptVersion;
        state.pageOffset = 0;
      } else if (state.transcriptLoadMode === "paged") {
        if (!Number.isInteger(state.pageLimit) || state.pageLimit <= 0) {
          state.pageLimit = 250;
        }
        await ensureFullTranscript(projectId, transcriptVersion);
      }
      const effectiveTranscript = state.fullTranscript || state.transcript;
      rebuildTokenState(effectiveTranscript, { force });
      renderTranscript(state.transcript);
      updatePageInfo(state.transcript?.pagination);
    }

    state.projectMetadata = metadataData;
    if (metadataData) {
      const mediaPath = metadataData.media_path || metadataData.mediaPath;
      state.currentMediaPath = mediaPath || null;
      state.currentMediaName = metadataData.media_name || metadataData.mediaName || null;
      updateMediaSource({ previewOnly: false });
    } else {
      state.currentMediaPath = null;
      state.currentMediaName = null;
      updateMediaSource({ previewOnly: true });
    }

    await refreshProjectFiles({ autoSelect: true });

    if (!state.currentProjectFileId) {
      const selectionData = await fetchSelection(projectId).catch(() => null);
      if (selectionData?.selection?.delete_ranges) {
        applyDeleteRanges(selectionData.selection.delete_ranges, { silent: true });
        state.selectionVersion = selectionData.version;
      } else {
        state.deletedKeys.clear();
        state.deleteRanges = [];
      }
      updateDeletedVisuals();
      resetHistory();
      updateTimelineSelection();
    }

    refreshTimelineWaveform();
    updateSilenceActionState();
  } catch (error) {
    logError(error);
  }
}

async function fetchTranscriptPreferred(projectId) {
  const params = new URLSearchParams();
  params.set("full", "1");
  try {
    const response = await fetch(`/api/projects/${projectId}/transcript?${params.toString()}`);
    if (response.ok) {
      const data = await response.json();
      return {
        mode: "full",
        data,
        sizeBytes: Number.isFinite(Number(data.size_bytes)) ? Number(data.size_bytes) : null,
      };
    }
  } catch (error) {
    logError(error);
  }
  logWarn("拉取完整转录失败，已回退到分页模式");
  const pagedData = await fetchTranscriptPage(projectId, state.pageOffset, state.pageLimit);
  return {
    mode: "paged",
    data: pagedData,
    sizeBytes: null,
    limitBytes: null,
  };
}

async function fetchTranscriptPage(projectId, offset, limit) {
  const params = new URLSearchParams();
  if (Number.isInteger(offset) && offset > 0) params.set("offset", String(offset));
  if (Number.isInteger(limit) && limit > 0) params.set("limit", String(limit));
  const response = await fetch(`/api/projects/${projectId}/transcript?${params.toString()}`);
  if (!response.ok) throw new Error(`获取转录失败: ${response.status}`);
  return response.json();
}

function formatFileSize(bytes) {
  const numeric = Number(bytes);
  if (!Number.isFinite(numeric) || numeric <= 0) {
    return "";
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = numeric;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  const decimals = value >= 10 || index === 0 ? 0 : 1;
  return `${value.toFixed(decimals)} ${units[index]}`;
}

async function fetchSelection(projectId, options = {}) {
  const params = new URLSearchParams();
  if (Number.isFinite(options.version)) {
    params.set("version", String(options.version));
  }
  const query = params.toString();
  const response = await fetch(
    `/api/projects/${projectId}/selection${query ? `?${query}` : ""}`,
  );
  if (!response.ok) throw new Error(`加载删除计划失败: ${response.status}`);
  return response.json();
}

async function fetchMetadata(projectId) {
  const response = await fetch(`/api/projects/${projectId}/metadata`);
  if (!response.ok) {
    if (response.status === 404) return null;
    throw new Error(`获取项目元数据失败: ${response.status}`);
  }
  const data = await response.json();
  return data?.metadata || null;
}

async function ensureFullTranscript(projectId, version) {
  if (!projectId) return null;
  if (state.fullTranscript && state.fullTranscriptVersion === version) {
    return state.fullTranscript;
  }
  try {
    const params = new URLSearchParams();
    params.set("full", "1");
    if (Number.isFinite(version)) {
      params.set("version", String(version));
    }
    const response = await fetch(`/api/projects/${projectId}/transcript?${params.toString()}`);
    if (response.status === 413) {
      state.fullTranscript = null;
      state.fullTranscriptVersion = null;
      return null;
    }
    if (!response.ok) throw new Error(`获取完整转录失败: ${response.status}`);
    const data = await response.json();
    state.fullTranscript = data.transcript || null;
    state.fullTranscriptVersion = data.version ?? null;
    return state.fullTranscript;
  } catch (error) {
    logError(error);
    state.fullTranscript = null;
    state.fullTranscriptVersion = null;
    return null;
  }
}
function handlePageSizeChange() {
  if (!dom.pageSizeInput) return;
  const value = Number(dom.pageSizeInput.value);
  if (Number.isInteger(value) && value > 0 && value !== state.pageLimit) {
    state.pageLimit = value;
    state.pageOffset = 0;
    loadCurrentProject({ force: true });
  }
}

function changePage(delta) {
  if (!state.transcript || !state.transcript.pagination) return;
  const pagination = state.transcript.pagination;
  const pageSize = pagination.limit || state.pageLimit || 250;
  const total = pagination.total_segments || 0;
  const nextOffset = Math.max(0, Math.min(pagination.offset + delta * pageSize, Math.max(0, total - pageSize)));
  if (nextOffset === pagination.offset) return;
  state.pageOffset = nextOffset;
  loadCurrentProject();
}

function updatePageInfo(pagination = {}) {
  if (!dom.pageInfo) return;
  const offset = pagination.offset || 0;
  const returned = pagination.returned || (state.transcript?.segments?.length ?? 0);
  const total = pagination.total_segments || returned;
  const limit = pagination.limit ?? (returned || state.pageLimit);
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(total, offset + returned);
  dom.pageInfo.textContent = `片段 ${start}-${end} / ${total}（每页 ${limit}）`;
}

// ---------------------------------------------------------------------------
// 转录渲染与令牌管理
// ---------------------------------------------------------------------------

function rebuildTokenState(transcript, options = {}) {
  if (!state.timelineController) return;
  const { force = false } = options;
  if (!transcript) {
    state.tokens = [];
    state.tokenMap.clear();
    state.wordNodeMap.clear();
    state.timelineData = null;
    state.timelineController.setData(null);
    state.timelineController.setDeleteRanges([]);
    return;
  }
  const timelineData = buildTimelineData(transcript);
  state.timelineData = timelineData;
  state.timelineController.setData(timelineData);
  state.timelineController.setDeleteRanges(buildDeleteRangesFromDeletedKeys());
  if (force || state.timelineDataVersion !== state.transcriptVersion || !state.timelineController.getZoom()) {
    state.timelineController.autoFit({ silent: true });
    state.timelineZoom = state.timelineController.getZoom();
  }
  state.timelineDataVersion = state.transcriptVersion;
  state.tokens = timelineData.tokens;
  state.tokenMap.clear();
  state.wordNodeMap.clear();
  timelineData.tokens.forEach((token) => {
    state.tokenMap.set(token.key, token);
  });
}

function renderTranscript(transcript) {
  if (!dom.segmentsContainer) return;
  dom.segmentsContainer.innerHTML = "";
  state.wordNodeMap.clear();

  if (!transcript || !Array.isArray(transcript.segments) || !transcript.segments.length) {
    const empty = document.createElement("div");
    empty.className = "segment empty";
    empty.textContent = "暂无转录内容";
    dom.segmentsContainer.appendChild(empty);
    return;
  }

  const offset = transcript.pagination?.offset || 0;
  let previousEnd = null;
  transcript.segments.forEach((segment, idx) => {
    const globalIndex = offset + idx;
    const { segmentEl, lastEnd } = buildSegmentElement(segment, globalIndex, previousEnd);
    previousEnd = lastEnd;
    dom.segmentsContainer.appendChild(segmentEl);
  });
  applyShowDeletedPreference();
  updateSelectionHighlights();
  updateSearchHighlights();
}

function buildSegmentElement(segment, globalIndex, previousEnd) {
  const segmentEl = document.createElement("div");
  segmentEl.className = "segment";
  segmentEl.dataset.segmentIndex = String(globalIndex);

  const header = document.createElement("div");
  header.className = "segment-header";
  const start = Number(segment.start) ?? 0;
  const end = Number(segment.end) ?? 0;
  header.textContent = `片段 #${globalIndex + 1} ｜ ${formatTime(start)} - ${formatTime(end)}`;

  const wordsContainer = document.createElement("div");
  wordsContainer.className = "words";
  const { tokens, lastEnd } = buildSegmentTokens(segment, globalIndex, previousEnd);
  tokens.forEach((token) => {
    if (!Number.isFinite(token.start) || !Number.isFinite(token.end) || token.end <= token.start) {
      return;
    }
    const normalized = normalizeToken(token, { segmentIndex: globalIndex });
    const node = buildTokenNode(normalized);
    wordsContainer.appendChild(node);
  });

  segmentEl.appendChild(header);
  segmentEl.appendChild(wordsContainer);
  return { segmentEl, lastEnd };
}

function normalizeToken(token, options = {}) {
  const normalized = {
    ...token,
  };
  if (normalized.segmentIndex == null && Number.isInteger(options.segmentIndex)) {
    normalized.segmentIndex = options.segmentIndex;
  }
  if (!normalized.key) {
    normalized.key = `tok:${normalized.segmentIndex}:${normalized.start.toFixed(3)}-${normalized.end.toFixed(3)}`;
  }
  state.tokenMap.set(normalized.key, normalized);
  return normalized;
}

function buildTokenNode(token) {
  const element = document.createElement("span");
  element.dataset.key = token.key;
  element.dataset.start = String(token.start);
  element.dataset.end = String(token.end);
  element.dataset.type = token.type || "word";
  element.dataset.segmentIndex = String(token.segmentIndex ?? 0);

  if (token.type === "word") {
    element.className = "word";
    element.textContent = token.text || "";
  } else if (token.type === "punctuation") {
    element.className = "punctuation";
    element.textContent = token.text || ",";
  } else if (token.type === "silence") {
    element.className = "silence-placeholder";
    element.textContent = "[...]";
  } else {
    element.className = "word";
    element.textContent = token.text || "";
  }

  state.wordNodeMap.set(token.key, element);
  if (state.deletedKeys.has(token.key)) {
    element.classList.add("deleted");
  }
  return element;
}

function clearTranscriptView() {
  if (dom.segmentsContainer) {
    dom.segmentsContainer.innerHTML = "";
  }
  state.tokenMap.clear();
  state.wordNodeMap.clear();
  state.tokens = [];
  state.selectedKeys.clear();
  state.deletedKeys.clear();
  state.deleteRanges = [];
  state.timelineActivePreviewKeys.clear();
  state.transcriptHoverKey = null;
  updateTimelineSelection();
  updateSearchHighlights();
}

function resetWorkspaceState(options = {}) {
  const { keepLocalPreview = false, preserveProjectId = false } = options;
  stopTaskPolling();
  state.currentTaskId = null;
  state.currentTaskType = null;
  if (!preserveProjectId) {
    state.currentProjectId = null;
  }
  state.transcript = null;
  state.transcriptVersion = null;
  state.fullTranscript = null;
  state.fullTranscriptVersion = null;
  state.selectionVersion = null;
  state.projectMetadata = null;
  state.currentMediaPath = null;
  state.currentMediaName = null;
  state.timelineData = null;
  state.timelineDataVersion = null;
  state.timelineWaveform = null;
  state.waveformSourceKey = null;
  state.mediaDuration = null;
  if (state.tokenFlashTimers && state.tokenFlashTimers.size) {
    state.tokenFlashTimers.forEach((timer, node) => {
      if (timer) {
        clearTimeout(timer);
      }
      if (node && node.classList) {
        node.classList.remove("focus-flash");
      }
    });
    state.tokenFlashTimers.clear();
  }
  state.selectionAnchor = null;
  state.history = [];
  state.future = [];
  state.deleteRanges = [];
  state.searchQuery = "";
  state.searchMatches = [];
  state.searchMatchSet.clear();
  state.searchPointer = 0;
  state.searchShouldScroll = false;
  clearSearchPreviewHighlight();
  clearTranscriptView();
  highlightPreviewKeys([], false);
  state.projectFiles = [];
  state.currentProjectFileId = null;
  state.currentProjectFileRevision = 0;
  state.openPanelProjectId = null;
  state.openPanelFiles = [];
  renderProjectFiles();
  renderProjects();
  renderOpenProjectFiles();
  if (state.timelineController) {
    state.timelineController.setData(null);
    state.timelineController.setDeleteRanges([]);
    state.timelineController.setWaveform(null);
    state.timelineController.setStatus("未选择项目");
  }
  if (!keepLocalPreview && state.localPreviewUrl) {
    URL.revokeObjectURL(state.localPreviewUrl);
    state.localPreviewUrl = null;
  }
  updateMediaSource({ previewOnly: true });
  updateSilenceActionState();
  if (dom.pageInfo) {
    dom.pageInfo.textContent = "未选择项目";
  }
  if (dom.taskStatus) {
    dom.taskStatus.textContent = "";
  }
  if (dom.cutStatus) {
    dom.cutStatus.textContent = "";
  }
  resetTranscribeButton();
  resetCutButton();
  if (dom.searchInput) {
    dom.searchInput.value = "";
  }
  if (dom.searchActions) {
    dom.searchActions.classList.remove("visible");
  }
}

function buildTimelineData(transcript) {
  const segments = Array.isArray(transcript.segments) ? transcript.segments : [];
  const offset = transcript.pagination?.offset || 0;
  const boundaries = new Set();
  const tokens = [];
  let previousEnd = null;

  segments.forEach((segment, idx) => {
    const globalIndex = offset + idx;
    const { tokens: segmentTokens, lastEnd } = buildSegmentTokens(segment, globalIndex, previousEnd);
    segmentTokens.forEach((token) => {
      if (!Number.isFinite(token.start) || !Number.isFinite(token.end) || token.end <= token.start) {
        return;
      }
      const normalized = {
        ...token,
        segmentIndex: token.segmentIndex ?? globalIndex,
        key: token.key,
        keys: [token.key],
      };
      boundaries.add(Number(normalized.start.toFixed(6)));
      boundaries.add(Number(normalized.end.toFixed(6)));
      if (!normalized.key) {
        normalized.key = `tok:${normalized.segmentIndex}:${normalized.start.toFixed(3)}-${normalized.end.toFixed(3)}`;
        normalized.keys = [normalized.key];
      }
      tokens.push(normalized);
    });
    previousEnd = lastEnd;
  });

  const duration = tokens.reduce((max, token) => Math.max(max, token.end), 0);
  return {
    tokens,
    boundaries: Array.from(boundaries).sort((a, b) => a - b),
    duration,
    offset,
  };
}

// ---------------------------------------------------------------------------
// 交互：选择、删除、撤销
// ---------------------------------------------------------------------------

function handleTranscriptClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const key = target.dataset.key;
  if (!key || !state.tokenMap.has(key)) return;

  const token = state.tokenMap.get(key);
  if (state.deletedKeys.has(key) && !state.hideDeleted) {
    // 点击已删除内容 -> 恢复
    restoreTokens([key], { announce: true });
    return;
  }

  if (event.shiftKey && state.selectionAnchor) {
    const anchorKeys = collectKeysBetween(state.selectionAnchor.key, key);
    if (anchorKeys.length) {
      toggleSelection(anchorKeys, { additive: true, mode: "add" });
      state.selectionAnchor = { key, token };
    }
    return;
  }

  toggleSelection([key], { additive: true, mode: "toggle" });
  state.selectionAnchor = { key, token };
}

function handleTranscriptDoubleClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;
  const key = target.dataset.key;
  if (!key) return;
  if (state.deletedKeys.has(key) && !state.hideDeleted) {
    restoreTokens([key], { announce: true });
    return;
  }
  const token = state.tokenMap.get(key);
  if (!token) return;
  event.preventDefault();
  event.stopPropagation();
  focusTranscriptToken(token);
}

function handleTranscriptKeydown(event) {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    handleSaveSelection();
    event.preventDefault();
  }
}

function handleTranscriptMouseUp(event) {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
    return;
  }
  const anchorNode = selection.anchorNode;
  const focusNode = selection.focusNode;
  if (
    !anchorNode ||
    !focusNode ||
    !dom.segmentsContainer?.contains(anchorNode) ||
    !dom.segmentsContainer.contains(focusNode)
  ) {
    return;
  }
  let range;
  try {
    const rawRange = selection.getRangeAt(0);
    range = rawRange.cloneRange ? rawRange.cloneRange() : rawRange;
  } catch (error) {
    return;
  }
  const keys = collectKeysWithinRange(range);
  if (!keys.length) {
    return;
  }
  const additive = event.ctrlKey || event.metaKey || event.shiftKey;
  toggleSelection(keys, { additive });
  state.selectionAnchor = { key: keys[0], token: state.tokenMap.get(keys[0]) };
  try {
    selection.removeAllRanges();
  } catch (error) {
    // ignore
  }
}

function collectKeysWithinRange(range) {
  const keys = [];
  if (!range) return keys;
  state.tokens.forEach((token) => {
    const node = state.wordNodeMap.get(token.key);
    if (!node || !node.isConnected) return;
    if (!dom.segmentsContainer?.contains(node)) return;
    if (state.hideDeleted && state.deletedKeys.has(token.key) && node.classList.contains("hidden-deleted")) return;
    if (rangeIntersectsNode(range, node)) {
      keys.push(token.key);
    }
  });
  return keys;
}

function rangeIntersectsNode(range, node) {
  try {
    const nodeRange = document.createRange();
    nodeRange.selectNodeContents(node);
    const END_TO_START = typeof Range !== "undefined" ? Range.END_TO_START : 3;
    const START_TO_END = typeof Range !== "undefined" ? Range.START_TO_END : 2;
    const intersects =
      range.compareBoundaryPoints(END_TO_START, nodeRange) < 0 &&
      range.compareBoundaryPoints(START_TO_END, nodeRange) > 0;
    nodeRange.detach?.();
    return intersects;
  } catch (error) {
    return false;
  }
}

function toggleSelection(keys, options = {}) {
  const { additive = false, mode = "toggle" } = options;
  if (!Array.isArray(keys) || !keys.length) return;
  const changedKeys = new Set();
  if (!additive && state.selectedKeys.size) {
    state.selectedKeys.forEach((key) => changedKeys.add(key));
    state.selectedKeys.clear();
  }
  keys.forEach((key) => {
    if (!state.tokenMap.has(key)) return;
    if (mode === "remove") {
      if (state.selectedKeys.delete(key)) {
        changedKeys.add(key);
      }
      return;
    }
    if (mode === "add" || (!additive && mode === "toggle")) {
      if (!state.selectedKeys.has(key)) {
        state.selectedKeys.add(key);
        changedKeys.add(key);
      }
      return;
    }
    if (mode === "toggle") {
      if (state.selectedKeys.has(key)) {
        state.selectedKeys.delete(key);
      } else {
        state.selectedKeys.add(key);
      }
      changedKeys.add(key);
    }
  });
  updateSelectionHighlights(changedKeys.size ? changedKeys : null);
  scheduleTimelineSelectionUpdate();
  if (!additive && keys.length) {
    scrollTokenIntoView(keys[keys.length - 1]);
  }
}

function collectKeysBetween(startKey, endKey) {
  if (!state.tokens.length) return [];
  const keyToIndex = new Map(state.tokens.map((token, index) => [token.key, index]));
  if (!keyToIndex.has(startKey) || !keyToIndex.has(endKey)) {
    return [endKey];
  }
  const startIndex = keyToIndex.get(startKey);
  const endIndex = keyToIndex.get(endKey);
  const [from, to] = startIndex <= endIndex ? [startIndex, endIndex] : [endIndex, startIndex];
  return state.tokens.slice(from, to + 1).map((token) => token.key);
}

function updateSelectionHighlights(changedKeys) {
  if (changedKeys && typeof changedKeys[Symbol.iterator] === "function") {
    const visited = new Set();
    for (const key of changedKeys) {
      if (key == null || visited.has(key)) continue;
      visited.add(key);
      const node = state.wordNodeMap.get(key);
      if (!node) continue;
      node.classList.toggle("selected", state.selectedKeys.has(key));
    }
    return;
  }
  state.wordNodeMap.forEach((node, key) => {
    node.classList.toggle("selected", state.selectedKeys.has(key));
  });
}

function clearSelection() {
  if (!state.selectedKeys.size && !state.selectionAnchor) {
    return;
  }
  const changedKeys = state.selectedKeys.size ? Array.from(state.selectedKeys) : null;
  state.selectedKeys.clear();
  state.selectionAnchor = null;
  if (changedKeys && changedKeys.length) {
    updateSelectionHighlights(changedKeys);
  }
  scheduleTimelineSelectionUpdate();
}

function deleteTokens(keys, options = {}) {
  if (!Array.isArray(keys) || !keys.length) return;
  let changed = false;
  keys.forEach((key) => {
    if (!state.tokenMap.has(key)) return;
    if (!state.deletedKeys.has(key)) {
      state.deletedKeys.add(key);
      changed = true;
    }
  });
  if (!changed) return;
  pushHistorySnapshot(`删除 ${keys.length} 个片段`);
  updateDeletedVisuals(keys);
  scheduleTimelineSelectionUpdate();
  updateSilenceActionState();
  clearSelection();
  const { reason } = options;
  if (reason === "search") {
    logInfo(`已删除 ${keys.length} 个匹配项`);
  }
}

function restoreTokens(keys, options = {}) {
  if (!Array.isArray(keys) || !keys.length) return;
  let changed = false;
  keys.forEach((key) => {
    if (state.deletedKeys.delete(key)) {
      changed = true;
    }
  });
  if (!changed) return;
  pushHistorySnapshot(`恢复 ${keys.length} 个片段`);
  updateDeletedVisuals(keys);
  scheduleTimelineSelectionUpdate();
  updateSilenceActionState();
  const { announce = false } = options;
  if (announce) {
    logInfo(`已恢复 ${keys.length} 个片段`);
  }
}

function applyDeleteRanges(ranges, options = {}) {
  if (!Array.isArray(ranges)) return;
  state.deletedKeys.clear();
  const keysToMark = [];
  ranges.forEach((range) => {
    const start = Number(range.start);
    const end = Number(range.end);
    state.tokens.forEach((token) => {
      if (rangeOverlap(token.start, token.end, start, end)) {
        keysToMark.push(token.key);
      }
    });
  });
  keysToMark.forEach((key) => state.deletedKeys.add(key));
  state.deleteRanges = ranges.map((range) => ({
    start: Number(range.start),
    end: Number(range.end),
  }));
  if (!options.silent) {
    updateDeletedVisuals();
    scheduleTimelineSelectionUpdate({ immediate: true });
  }
}

function updateDeletedVisuals(keys) {
  const targetKeys = keys && keys.length ? keys : Array.from(state.tokenMap.keys());
  targetKeys.forEach((key) => {
    const node = state.wordNodeMap.get(key);
    if (!node) return;
    if (state.deletedKeys.has(key)) {
      node.classList.add("deleted");
      if (state.hideDeleted) {
        node.classList.add("hidden-deleted");
      }
    } else {
      node.classList.remove("deleted");
      node.classList.remove("hidden-deleted");
    }
  });
}

function applyShowDeletedPreference() {
  state.wordNodeMap.forEach((node, key) => {
    if (state.deletedKeys.has(key)) {
      node.classList.toggle("hidden-deleted", state.hideDeleted);
    }
  });
}

function handleToggleShowDeleted() {
  state.hideDeleted = !dom.showDeletedToggle?.checked;
  applyShowDeletedPreference();
}

function handleKeydown(event) {
  if (event.repeat) return;
  // BEGIN-EDIT
  if (event.key === " " && !event.ctrlKey && !event.metaKey && !event.altKey) {
    if (tryFocusTokenFromHover()) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (shouldHandleTimelineSpace(event)) {
      event.preventDefault();
      triggerTimelinePlayback();
      return;
    }
  }
  // END-EDIT
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
    event.preventDefault();
    if (event.shiftKey) {
      redo();
    } else {
      undo();
    }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
    event.preventDefault();
    handleSaveSelection();
    return;
  }
  if (event.key === "Delete" || event.key === "Backspace") {
    if (state.selectedKeys.size) {
      deleteTokens(Array.from(state.selectedKeys));
      event.preventDefault();
    }
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    const addedKeys = [];
    state.tokens.forEach((token) => {
      if (!token || token.key == null) return;
      if (!state.selectedKeys.has(token.key)) {
        state.selectedKeys.add(token.key);
        addedKeys.push(token.key);
      }
    });
    if (addedKeys.length) {
      updateSelectionHighlights(addedKeys);
      scheduleTimelineSelectionUpdate();
    }
    return;
  }
}

function applyHistorySnapshot(snapshot, direction) {
  if (!snapshot) return;
  state.deletedKeys = new Set(snapshot.deletedKeys);
  state.selectedKeys = new Set(snapshot.selectedKeys);
  updateDeletedVisuals();
  updateSelectionHighlights();
  scheduleTimelineSelectionUpdate({ immediate: true });
  updateSilenceActionState();
  if (state.searchQuery) {
    performSearch(state.searchQuery, { silent: true });
  } else {
    updateSearchHighlights();
  }
  const prefix = direction === "redo" ? "重做" : "撤销";
  logInfo(`${prefix}: ${snapshot.description}`);
}

function undo() {
  if (state.history.length <= 1) return;
  const snapshot = state.history.pop();
  if (!snapshot) return;
  state.future.push(snapshot);
  const previous = state.history[state.history.length - 1];
  applyHistorySnapshot(previous, "undo");
}

function redo() {
  if (!state.future.length) return;
  const snapshot = state.future.pop();
  if (!snapshot) return;
  state.history.push(snapshot);
  applyHistorySnapshot(snapshot, "redo");
}

function pushHistorySnapshot(description) {
  const snapshot = captureHistorySnapshot(description);
  state.history.push(snapshot);
  if (state.history.length > MAX_HISTORY) {
    state.history.shift();
  }
  state.future = [];
}

function resetHistory(description = "初始状态") {
  const snapshot = captureHistorySnapshot(description);
  state.history = [snapshot];
  state.future = [];
}

function captureHistorySnapshot(description) {
  return {
    description,
    deletedKeys: Array.from(state.deletedKeys),
    selectedKeys: Array.from(state.selectedKeys),
  };
}

function buildDeleteRangesFromDeletedKeys() {
  const ranges = [];
  const sortedTokens = Array.from(state.deletedKeys)
    .map((key) => state.tokenMap.get(key))
    .filter(Boolean)
    .sort((a, b) => a.start - b.start);
  let current = null;
  sortedTokens.forEach((token) => {
    if (!current) {
      current = { start: token.start, end: token.end };
      return;
    }
    if (token.start <= current.end + DELETE_EPSILON) {
      current.end = Math.max(current.end, token.end);
    } else {
      ranges.push(current);
      current = { start: token.start, end: token.end };
    }
  });
  if (current) {
    ranges.push(current);
  }
  state.deleteRanges = ranges;
  return ranges;
}

function updateTimelineSelection() {
  if (!state.timelineController) return;
  const ranges = buildDeleteRangesFromDeletedKeys();
  state.timelineController.setDeleteRanges(ranges);
}

function scheduleTimelineSelectionUpdate(options = {}) {
  if (!state.timelineController) return;
  const { immediate = false } = options;
  if (immediate || typeof window === "undefined") {
    state.timelineSelectionScheduled = false;
    updateTimelineSelection();
    return;
  }
  if (state.timelineSelectionScheduled) return;
  state.timelineSelectionScheduled = true;
  const scheduler =
    window.requestAnimationFrame ||
    window.webkitRequestAnimationFrame ||
    window.mozRequestAnimationFrame ||
    window.msRequestAnimationFrame ||
    ((callback) => setTimeout(callback, 16));
  scheduler(() => {
    state.timelineSelectionScheduled = false;
    updateTimelineSelection();
  });
}

async function persistSelection(deleteRanges, options = {}) {
  if (!state.currentProjectId) {
    throw new Error('暂无选中的项目');
  }
  if (!Array.isArray(deleteRanges) || !deleteRanges.length) {
    return { skipped: true };
  }
  const metadata = {
    updated_at: new Date().toISOString(),
  };
  const trimmedLabel = typeof options.label === 'string' ? options.label.trim() : '';
  if (trimmedLabel) {
    metadata.label = trimmedLabel;
  }
  if (options.note) {
    metadata.note = String(options.note);
  }
  if (Number.isFinite(options.baseVersion)) {
    metadata.base_version = options.baseVersion;
  } else if (Number.isFinite(state.selectionVersion)) {
    metadata.base_version = state.selectionVersion;
  }
  const payload = {
    delete_ranges: deleteRanges,
    metadata,
  };
  if (Number.isInteger(state.currentProjectFileId)) {
    await saveProjectFileState(state.currentProjectFileId, payload, options);
  }
  const response = await fetch(`/api/projects/${state.currentProjectId}/selection`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(`保存删除计划失败: ${response.status}`);
  }
  const data = await response.json();
  state.selectionVersion = data.version;
  if (state.selectionSnapshotDialogOpen) {
    queueMicrotask(() => refreshSnapshotList());
  }
  if (Number.isInteger(state.currentProjectFileId)) {
    queueMicrotask(() => refreshProjectFiles({ autoSelect: false }));
  }
  queueMicrotask(() => refreshProjects());
  return data;
}

async function saveProjectFileState(projectFileId, selectionPayload, options = {}) {
  const body = { selection: selectionPayload };
  if (options && typeof options.name === 'string') {
    const trimmedName = options.name.trim();
    if (trimmedName) {
      body.name = trimmedName;
    }
  }
  const response = await fetch(`/api/project-files/${projectFileId}/save`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`保存工程文件失败: ${response.status}`);
  }
  const data = await response.json();
  const file = data?.file;
  if (file) {
    state.currentProjectFileRevision = Number(file.revision) || state.currentProjectFileRevision;
    if (Array.isArray(state.projectFiles)) {
      const nextFiles = state.projectFiles.slice();
      const index = nextFiles.findIndex((item) => item.id === file.id);
      if (index >= 0) {
        nextFiles[index] = file;
      }
      state.projectFiles = nextFiles;
      renderProjectFiles();
    }
  }
  return data;
}

function normalizeSelectionSnapshot(entry) {
  if (!entry || typeof entry !== "object") {
    return null;
  }
  const payload = entry.payload || {};
  const metadata = payload.metadata || {};
  return {
    version: Number(entry.version) || 0,
    createdAt: entry.created_at || "",
    label: typeof metadata.label === "string" && metadata.label.trim() ? metadata.label.trim() : "",
    note: typeof metadata.note === "string" ? metadata.note : "",
    baseVersion: Number(metadata.base_version),
    deleteRanges: Array.isArray(payload.delete_ranges) ? payload.delete_ranges : [],
  };
}

async function refreshSnapshotList() {
  if (!state.currentProjectId) {
    return;
  }
  state.selectionSnapshotLoading = true;
  state.selectionSnapshotError = null;
  renderSnapshotList();
  try {
    const response = await fetch(`/api/projects/${state.currentProjectId}/snapshots`);
    if (!response.ok) {
      throw new Error(`加载版本信息失败: ${response.status}`);
    }
    const data = await response.json();
    const selectionEntries = Array.isArray(data?.selections) ? data.selections : [];
    const normalized = selectionEntries
      .map((entry) => normalizeSelectionSnapshot(entry))
      .filter((item) => item && Number.isFinite(item.version));
    normalized.sort((a, b) => b.version - a.version);
    state.selectionSnapshots = normalized;
  } catch (error) {
    state.selectionSnapshotError = error;
    logError(error);
  } finally {
    state.selectionSnapshotLoading = false;
    renderSnapshotList();
  }
}

function renderSnapshotList() {
  if (!dom.snapshotList) return;
  dom.snapshotList.innerHTML = "";
  if (state.selectionSnapshotLoading) {
    const item = document.createElement("li");
    item.className = "snapshot-item snapshot-item--loading";
    item.textContent = "正在加载版本...";
    dom.snapshotList.appendChild(item);
    dom.snapshotEmpty?.classList.add("hidden");
    return;
  }
  if (state.selectionSnapshotError) {
    const item = document.createElement("li");
    item.className = "snapshot-item snapshot-item--error";
    item.textContent = state.selectionSnapshotError.message || "加载版本信息失败";
    dom.snapshotList.appendChild(item);
    dom.snapshotEmpty?.classList.add("hidden");
    return;
  }
  const snapshots = Array.isArray(state.selectionSnapshots) ? state.selectionSnapshots : [];
  if (!snapshots.length) {
    dom.snapshotEmpty?.classList.remove("hidden");
    return;
  }
  dom.snapshotEmpty?.classList.add("hidden");
  snapshots.forEach((snapshot) => {
    const item = document.createElement("li");
    item.className = "snapshot-item";
    item.dataset.version = String(snapshot.version);
    if (snapshot.version === state.selectionVersion) {
      item.classList.add("snapshot-item--active");
    }

    const title = document.createElement("div");
    title.className = "snapshot-item__title";
    title.textContent = snapshot.label || `版本 ${snapshot.version}`;

    const meta = document.createElement("div");
    meta.className = "snapshot-item__meta";
    const createdAt = snapshot.createdAt ? new Date(snapshot.createdAt) : null;
    const datetime = createdAt && !Number.isNaN(createdAt.valueOf())
      ? createdAt.toLocaleString()
      : "未知时间";
    const baseInfo = Number.isFinite(snapshot.baseVersion) ? `基于 #${snapshot.baseVersion}` : "";
    meta.textContent = baseInfo ? `${datetime} · ${baseInfo}` : datetime;

    const actionBar = document.createElement("div");
    actionBar.className = "snapshot-item__actions";
    const loadButton = document.createElement("button");
    loadButton.type = "button";
    loadButton.className = "ghost-button snapshot-item__action";
    loadButton.dataset.action = "load";
    loadButton.dataset.version = String(snapshot.version);
    if (snapshot.version === state.selectionVersion) {
      loadButton.disabled = true;
      loadButton.textContent = "已加载";
    } else {
      loadButton.textContent = "加载版本";
    }
    actionBar.appendChild(loadButton);

    item.appendChild(title);
    item.appendChild(meta);
    if (snapshot.note) {
      const note = document.createElement("div");
      note.className = "snapshot-item__note";
      note.textContent = snapshot.note;
      item.appendChild(note);
    }
    item.appendChild(actionBar);
    dom.snapshotList.appendChild(item);
  });
}

async function openSnapshotPanel() {
  if (!state.currentProjectId) {
    logError(new Error("暂无选中的项目"));
    return;
  }
  state.selectionSnapshotDialogOpen = true;
  if (dom.snapshotPanel) {
    dom.snapshotPanel.classList.add("open");
    dom.snapshotPanel.setAttribute("aria-hidden", "false");
  }
  if (dom.snapshotBackdrop) {
    dom.snapshotBackdrop.classList.add("open");
    dom.snapshotBackdrop.setAttribute("aria-hidden", "false");
  }
  await refreshSnapshotList();
  if (dom.snapshotCreate) {
    dom.snapshotCreate.focus();
  }
}

function closeSnapshotPanel() {
  if (!dom.snapshotPanel?.classList.contains("open")) {
    state.selectionSnapshotDialogOpen = false;
    return false;
  }
  dom.snapshotPanel.classList.remove("open");
  dom.snapshotPanel.setAttribute("aria-hidden", "true");
  dom.snapshotBackdrop?.classList.remove("open");
  dom.snapshotBackdrop?.setAttribute("aria-hidden", "true");
  state.selectionSnapshotDialogOpen = false;
  return true;
}

async function handleSnapshotCreate() {
  if (!state.currentProjectId) {
    logError(new Error("暂无选中的项目"));
    return;
  }
  const deleteRanges = buildDeleteRangesFromDeletedKeys();
  const defaultLabel = `版本 ${state.selectionVersion ? state.selectionVersion + 1 : 1}`;
  const label = window.prompt("为当前编辑保存一个版本，请输入名称：", defaultLabel);
  if (label == null) {
    return;
  }
  try {
    await persistSelection(deleteRanges, { label, baseVersion: state.selectionVersion });
    logInfo("版本已保存");
    await refreshSnapshotList();
  } catch (error) {
    logError(error);
  }
}

function handleSnapshotAction(event) {
  const button = event.target instanceof HTMLElement ? event.target.closest("button[data-action]") : null;
  if (!button) return;
  const action = button.dataset.action;
  const version = Number(button.dataset.version);
  if (!Number.isFinite(version)) {
    return;
  }
  if (action === "load") {
    loadSnapshotVersion(version);
  }
}

async function loadSnapshotVersion(version) {
  if (!state.currentProjectId) {
    logError(new Error("暂无选中的项目"));
    return;
  }
  try {
    const data = await fetchSelection(state.currentProjectId, { version });
    if (data?.selection?.delete_ranges) {
      applyDeleteRanges(data.selection.delete_ranges, { silent: true });
      state.selectionVersion = data.version;
      updateDeletedVisuals();
      updateTimelineSelection();
      refreshTimelineWaveform();
      updateSilenceActionState();
      resetHistory("加载版本");
      logInfo(`已加载版本 #${version}`);
      renderSnapshotList();
      closeSnapshotPanel();
    } else {
      logWarn("指定版本没有删除计划数据");
    }
  } catch (error) {
    logError(error);
  }
}

function isRangeSelected(start, end) {
  return state.deleteRanges.some((range) => rangeOverlap(range.start, range.end, start, end));
}

function rangeOverlap(aStart, aEnd, bStart, bEnd) {
  const start = Math.max(aStart, bStart);
  const end = Math.min(aEnd, bEnd);
  return end - start > DELETE_EPSILON;
}

function scrollTokenIntoView(key) {
  const node = state.wordNodeMap.get(key);
  if (!node) return;
  node.scrollIntoView({ block: "center", inline: "nearest", behavior: "smooth" });
}

function flashTokens(keys) {
  if (!Array.isArray(keys) || !keys.length) return;
  if (!(state.tokenFlashTimers instanceof Map)) {
    state.tokenFlashTimers = new Map();
  }
  keys.forEach((rawKey) => {
    const key = rawKey == null ? null : String(rawKey);
    if (!key) return;
    const node = state.wordNodeMap.get(key);
    if (!node) return;
    const previousTimer = state.tokenFlashTimers.get(node);
    if (previousTimer) {
      clearTimeout(previousTimer);
    }
    node.classList.add("focus-flash");
    const timer = window.setTimeout(() => {
      node.classList.remove("focus-flash");
      state.tokenFlashTimers.delete(node);
    }, 900);
    state.tokenFlashTimers.set(node, timer);
  });
}

function focusTranscriptKeys(keys) {
  if (!Array.isArray(keys) || !keys.length) return;
  const normalized = Array.from(
    new Set(
      keys
        .map((key) => (key == null ? null : String(key)))
        .filter((key) => key && state.wordNodeMap.has(key))
    )
  );
  if (!normalized.length) return;
  const targetKey = normalized[0];
  scrollTokenIntoView(targetKey);
  flashTokens(normalized);
  state.selectionAnchor = { key: targetKey, token: state.tokenMap.get(targetKey) };
}

function focusTimelineRange(range, token, options = {}) {
  const startTime = Number(range?.start);
  if (!Number.isFinite(startTime)) {
    return;
  }
  const clampedStart = Math.max(0, startTime);
  const endTimeRaw = Number(range?.end);
  const clampedEnd = Number.isFinite(endTimeRaw) ? Math.max(clampedStart, endTimeRaw) : clampedStart;
  if (state.timelineController) {
    state.timelineController.focusRange({ start: clampedStart, end: clampedEnd }, token, {
      behavior: options.behavior || "smooth",
      seek: options.seek !== false,
    });
  } else if (dom.mediaPlayer && Number.isFinite(dom.mediaPlayer.duration)) {
    dom.mediaPlayer.currentTime = Math.min(clampedStart, dom.mediaPlayer.duration);
  }
}

function focusTranscriptToken(token) {
  if (!token) return;
  const start = Number(token.start);
  if (!Number.isFinite(start)) return;
  const end = Number.isFinite(token.end) ? Math.max(start, token.end) : start;
  const keys = Array.isArray(token.keys) && token.keys.length ? token.keys : [token.key];
  focusTimelineRange({ start, end }, token, { behavior: "smooth" });
  focusTranscriptKeys(keys);
}

function handleTimelineFocusRange(payload) {
  if (!payload) return;
  const token = payload.token ?? null;
  const range = payload.range ?? (token ? { start: token.start, end: token.end } : null);
  if (range && state.timelineController) {
    // ensure playhead/time already updated by controller; just emphasize transcript
    const keysSource =
      Array.isArray(payload.keys) && payload.keys.length
        ? payload.keys
        : Array.isArray(token?.keys) && token.keys.length
        ? token.keys
        : token?.key
        ? [token.key]
        : [];
    focusTranscriptKeys(keysSource);
  }
}

function tryFocusTokenFromHover() {
  const hoverKey = state.transcriptHoverKey;
  if (!hoverKey) return false;
  const active = document.activeElement;
  if (active && (active.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName))) {
    return false;
  }
  const token = state.tokenMap.get(hoverKey);
  if (!token) return false;
  focusTranscriptToken(token);
  return true;
}

// ---------------------------------------------------------------------------
// 搜索与批量操作
// ---------------------------------------------------------------------------

function handleSearchInput(event) {
  const value = (event.target?.value || "").trim();
  performSearch(value);
}

function normalizeSearchString(text) {
  return String(text || "").trim().toLowerCase();
}

function normalizedTokenText(token, options = {}) {
  if (!token) return "";
  const raw = typeof token.text === "string" ? token.text : "";
  const processed = options.stripSpaces ? raw.replace(/\s+/g, "") : raw;
  return processed.toLowerCase();
}

function isTokenEligibleForMatching(token, includeDeleted = false) {
  if (!token || token.type !== "word") return false;
  if (!includeDeleted && state.deletedKeys.has(token.key)) return false;
  const textValue = typeof token.text === "string" ? token.text.trim() : "";
  return Boolean(textValue);
}

function findTokenSequencesMatchingText(query, options = {}) {
  const trimmed = String(query || "").trim();
  if (!trimmed) return [];
  const includeDeleted = Boolean(options.includeDeleted);
  const tokens = Array.isArray(state.tokens) ? state.tokens : [];
  if (!tokens.length) return [];

  const parts = trimmed.split(/\s+/).filter(Boolean);
  if (parts.length > 1) {
    const normalizedParts = parts.map((part) => normalizeSearchString(part));
    return matchTokensByParts(tokens, normalizedParts, includeDeleted);
  }
  const normalized = normalizeSearchString(trimmed).replace(/\s+/g, "");
  if (!normalized) return [];
  return matchTokensByConcatenation(tokens, normalized, includeDeleted);
}

function matchTokensByParts(tokens, parts, includeDeleted) {
  const matches = [];
  const maxStart = tokens.length - parts.length;
  for (let start = 0; start <= maxStart; start += 1) {
    let matched = true;
    const keys = [];
    for (let offset = 0; offset < parts.length; offset += 1) {
      const token = tokens[start + offset];
      if (!isTokenEligibleForMatching(token, includeDeleted)) {
        matched = false;
        break;
      }
      if (normalizedTokenText(token) !== parts[offset]) {
        matched = false;
        break;
      }
      keys.push(token.key);
    }
    if (matched) {
      matches.push({ keys, startIndex: start, endIndex: start + parts.length - 1 });
    }
  }
  return matches;
}

function matchTokensByConcatenation(tokens, normalizedQuery, includeDeleted) {
  const matches = [];
  for (let start = 0; start < tokens.length; start += 1) {
    const first = tokens[start];
    if (!isTokenEligibleForMatching(first, includeDeleted)) {
      continue;
    }
    const firstText = normalizedTokenText(first, { stripSpaces: true });
    if (!firstText || !normalizedQuery.startsWith(firstText)) {
      continue;
    }
    const keys = [first.key];
    if (firstText === normalizedQuery) {
      matches.push({ keys: [...keys], startIndex: start, endIndex: start });
      continue;
    }
    let composed = firstText;
    for (let index = start + 1; index < tokens.length; index += 1) {
      const token = tokens[index];
      if (!isTokenEligibleForMatching(token, includeDeleted)) {
        break;
      }
      const segment = normalizedTokenText(token, { stripSpaces: true });
      if (!segment) break;
      const next = composed + segment;
      if (!normalizedQuery.startsWith(next)) {
        break;
      }
      keys.push(token.key);
      composed = next;
      if (composed === normalizedQuery) {
        matches.push({ keys: [...keys], startIndex: start, endIndex: index });
        break;
      }
    }
  }
  return matches;
}

function performSearch(query, options = {}) {
  const { silent = false } = options;
  state.searchQuery = query;
  state.searchMatches = [];
  state.searchMatchSet.clear();
  state.searchPointer = 0;
  clearSearchPreviewHighlight();
  if (!query) {
    updateSearchHighlights();
    dom.searchActions?.classList.remove("visible");
    return;
  }
  const sequences = findTokenSequencesMatchingText(query);
  if (!sequences.length) {
    if (!silent) {
      logInfo("未找到匹配内容");
    }
    dom.searchActions?.classList.remove("visible");
    updateSearchHighlights();
    return;
  }
  dom.searchActions?.classList.add("visible");
  state.searchPointer = 0;
  state.searchMatches = sequences.map((sequence) => sequence.keys);
  state.searchMatchSet = new Set(sequences.flatMap((sequence) => sequence.keys));
  const firstSequence = state.searchMatches[0];
  if (firstSequence?.length) {
    scrollTokenIntoView(firstSequence[0]);
  }
  updateSearchHighlights();
}

function updateSearchHighlights() {
  state.wordNodeMap.forEach((node, key) => {
    node.classList.toggle("search-hit", state.searchMatchSet.has(key));
    node.classList.remove("search-active");
  });
  if (state.searchMatches.length) {
    const activeSequence =
      state.searchMatches[Math.min(state.searchPointer, state.searchMatches.length - 1)] || [];
    const firstKey = activeSequence[0];
    const activeNode = firstKey ? state.wordNodeMap.get(firstKey) : null;
    if (activeNode) {
      activeNode.classList.add("search-active");
    }
  }
}

function deleteSearchMatches(deleteAll) {
  if (!state.searchMatches.length) return;
  const targetIndex = Math.min(state.searchPointer, state.searchMatches.length - 1);
  const sequences = deleteAll
    ? state.searchMatches.slice()
    : state.searchMatches[targetIndex]
      ? [state.searchMatches[targetIndex]]
      : [];
  if (!sequences.length) return;
  const keysToDelete = new Set();
  sequences.forEach((sequence) => {
    sequence.forEach((key) => keysToDelete.add(key));
  });
  deleteTokens(Array.from(keysToDelete), { reason: "search" });
  performSearch(state.searchQuery);
}

function handleSearchSkip() {
  if (!state.searchMatches.length) {
    return;
  }
  clearSearchPreviewHighlight();
  moveSearchPointer(1);
  if (dom.searchActions) {
    dom.searchActions.classList.add("visible");
  }
}

function handleSearchKeydown(event) {
  if (!state.searchMatches.length) return;
  if (event.key === "Enter") {
    event.preventDefault();
    const direction = event.shiftKey ? -1 : 1;
    moveSearchPointer(direction);
  }
}

function moveSearchPointer(direction) {
  if (!state.searchMatches.length) return;
  state.searchPointer =
    (state.searchPointer + direction + state.searchMatches.length) % state.searchMatches.length;
  updateSearchHighlights();
  const sequence = state.searchMatches[state.searchPointer];
  if (sequence && sequence.length) {
    scrollTokenIntoView(sequence[0]);
  }
}

function clearSearchPreviewHighlight() {
  if (!state.searchPreviewKeys.size) return;
  state.searchPreviewKeys.forEach((key) => {
    const node = state.wordNodeMap.get(key);
    if (node) {
      node.classList.remove("preview-delete");
    }
  });
  state.searchPreviewKeys.clear();
}

function highlightSearchPreview(active, highlightAll = false) {
  clearSearchPreviewHighlight();
  if (!active) return;
  if (!state.searchMatches.length) return;
  const targetIndex = Math.min(state.searchPointer, state.searchMatches.length - 1);
  const sequences = highlightAll
    ? state.searchMatches
    : state.searchMatches[targetIndex]
      ? [state.searchMatches[targetIndex]]
      : [];
  if (!sequences.length) return;
  sequences.forEach((sequence) => {
    sequence.forEach((key) => {
      const node = state.wordNodeMap.get(key);
      if (node) {
        node.classList.add("preview-delete");
        state.searchPreviewKeys.add(key);
      }
    });
  });
}

function handleFillerInput(event) {
  const raw = (event.target?.value || "").trim();
  state.cachedFillerQuery = raw;
  state.cachedFillerMatches = [];
  highlightPreviewKeys([], false);
  updateSilenceActionState();
}

function parseRawFillerList(raw) {
  if (!raw) return [];
  const tokens = raw
    .split(/[\s,，；;、]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  return Array.from(new Set(tokens));
}

function getFillerMatches() {
  const query = (state.cachedFillerQuery || dom.fillerWordsInput?.value || "").trim();
  const mergedList = parseRawFillerList(query || "");
  const dictionary = mergedList.length ? mergedList : state.commonFillerWords;
  if (!dictionary.length) return [];
  const matchedTokens = new Map();
  dictionary.forEach((phrase) => {
    const sequences = findTokenSequencesMatchingText(phrase, { includeDeleted: false });
    sequences.forEach((sequence) => {
      sequence.keys.forEach((key) => {
        if (state.deletedKeys.has(key)) return;
        const token = state.tokenMap.get(key);
        if (token) {
          matchedTokens.set(key, token);
        }
      });
    });
  });
  return Array.from(matchedTokens.values());
}

function highlightFillerPreview(active) {
  const matches = getFillerMatches();
  matches.forEach((token) => {
    const node = state.wordNodeMap.get(token.key);
    if (!node) return;
    node.classList.toggle("preview-delete", Boolean(active));
  });
  if (!active) {
    state.wordNodeMap.forEach((node) => node.classList.remove("preview-delete"));
  }
}

function applyFillerWordsAndSave() {
  const matches = getFillerMatches();
  if (!matches.length) {
    logInfo("未找到可删除的水词");
    return;
  }
  deleteTokens(matches.map((token) => token.key));
  logInfo(`已删除 ${matches.length} 个水词`);
}

function highlightSilencePlaceholders(active) {
  state.wordNodeMap.forEach((node, key) => {
    const token = state.tokenMap.get(key);
    if (!token || token.type !== "silence") return;
    node.classList.toggle("preview-delete", Boolean(active));
  });
  if (!active) {
    state.wordNodeMap.forEach((node) => node.classList.remove("preview-delete"));
  }
}

function handleDeleteAllSilencePlaceholders() {
  const silenceKeys = state.tokens.filter((token) => token.type === "silence").map((token) => token.key);
  if (!silenceKeys.length) {
    logInfo("没有静音占位可以删除");
    return;
  }
  deleteTokens(silenceKeys);
  logInfo(`已删除 ${silenceKeys.length} 个静音占位符`);
}

function updateSilenceActionState() {
  const hasSilence = state.tokens.some((token) => token.type === "silence" && !state.deletedKeys.has(token.key));
  if (dom.manuscriptDeleteSilence) {
    dom.manuscriptDeleteSilence.disabled = !hasSilence;
  }
  const fillerMatches = getFillerMatches();
  if (dom.manuscriptDeleteFiller) {
    dom.manuscriptDeleteFiller.disabled = !fillerMatches.length;
  }
}

// ---------------------------------------------------------------------------
// 时间轴联动
// ---------------------------------------------------------------------------

function highlightPreviewKeys(keys, active) {
  let previousKeys = state.timelineActivePreviewKeys;
  if (!(previousKeys instanceof Set)) {
    previousKeys = new Set();
  }
  const normalizedKeys =
    active && Array.isArray(keys)
      ? keys
          .map((key) => (key == null ? null : String(key)))
          .filter((key) => key && state.wordNodeMap.has(key))
      : [];
  const nextKeySet = new Set(normalizedKeys);

  previousKeys.forEach((storedKey) => {
    if (!nextKeySet.has(storedKey)) {
      const storedNode = state.wordNodeMap.get(storedKey);
      if (storedNode) {
        storedNode.classList.remove("marked");
      }
    }
  });

  if (!active || normalizedKeys.length === 0) {
    state.timelineActivePreviewKeys = nextKeySet;
    if (previousKeys.size === 0 && nextKeySet.size === 0) {
      state.wordNodeMap.forEach((node) => node.classList.remove("marked"));
    }
    return;
  }

  normalizedKeys.forEach((key) => {
    const node = state.wordNodeMap.get(key);
    if (node) {
      node.classList.add("marked");
    }
  });

  state.timelineActivePreviewKeys = nextKeySet;
}

function handleTranscriptPointerMove(event) {
  if (event.pointerType === "touch") return;
  if (event.buttons && event.buttons !== 0) return;

  const target = event.target instanceof HTMLElement ? event.target.closest("[data-key]") : null;
  if (!target || !dom.segmentsContainer?.contains(target)) {
    if (state.transcriptHoverKey != null) {
      clearTranscriptHoverFeedback();
    }
    return;
  }

  const key = target.dataset.key;
  if (!key) {
    if (state.transcriptHoverKey != null) {
      clearTranscriptHoverFeedback();
    }
    return;
  }
  if (state.transcriptHoverKey === key) {
    return;
  }
  if (!state.tokenMap.has(key)) {
    clearTranscriptHoverFeedback();
    return;
  }

  const token = state.tokenMap.get(key);
  const previewKeys =
    Array.isArray(token?.keys) && token.keys.length
      ? token.keys
          .map((item) => (item == null ? null : String(item)))
          .filter((value) => value && state.wordNodeMap.has(value))
      : [key];

  state.transcriptHoverKey = key;
  highlightPreviewKeys(previewKeys, true);

  if (
    state.timelineController &&
    !state.timelineController.scrubbing &&
    Number.isFinite(token?.start) &&
    Number.isFinite(token?.end)
  ) {
    const start = Math.max(0, token.start);
    const end = Math.max(start, token.end);
    state.timelineController.setPreview({ start, end }, { keys: previewKeys }, false);
  }
}

function handleTranscriptPointerLeave(event) {
  if (event.pointerType === "touch") return;
  clearTranscriptHoverFeedback();
}

function handleTranscriptPointerDown(event) {
  if (event.pointerType === "touch") return;
  if (event.button !== 0) return;
  if (!dom.segmentsContainer?.contains(event.target)) return;
  clearTranscriptHoverFeedback();
}

function clearTranscriptHoverFeedback() {
  if (state.transcriptHoverKey == null && (!state.timelineActivePreviewKeys || !state.timelineActivePreviewKeys.size)) {
    return;
  }
  state.transcriptHoverKey = null;
  highlightPreviewKeys([], false);
  if (state.timelineController && !state.timelineController.scrubbing) {
    state.timelineController.clearPreview();
  }
}

function handleTimelineScrub(time) {
  // BEGIN-EDIT
  state.timelineLastInteractionAt = Date.now();
  if (Number.isFinite(time)) {
    state.timelineLastScrubTime = Math.max(0, time);
  }
  state.segmentPlaybackEnd = null;
  state.timelinePendingRange = null;
  state.timelinePendingRangeTimestamp = 0;
  // END-EDIT
  if (dom.mediaPlayer && Number.isFinite(dom.mediaPlayer.duration)) {
    dom.mediaPlayer.currentTime = Math.min(Math.max(time, 0), dom.mediaPlayer.duration);
  }
}

function handleTimelineToggle(range, context = {}) {
  if (!range) return;
  const keys = Array.isArray(context.keys) && context.keys.length ? context.keys : findKeysByRange(range.start, range.end);
  if (!keys.length) return;
  const additive = Boolean(context.additive);
  toggleSelection(keys, { additive });
  if (!additive) {
    state.selectionAnchor = { key: keys[0], token: state.tokenMap.get(keys[0]) };
  }
  // BEGIN-EDIT
  state.timelineLastInteractionAt = Date.now();
  state.timelinePendingRange = {
    start: Number.isFinite(range.start) ? Math.max(0, range.start) : 0,
    end: Number.isFinite(range.end) ? Math.max(Number(range.end), 0) : 0,
  };
  if (state.timelinePendingRange.end <= state.timelinePendingRange.start) {
    state.timelinePendingRange = null;
    state.timelinePendingRangeTimestamp = 0;
  } else {
    state.timelinePendingRangeTimestamp = Date.now();
  }
  // END-EDIT
}

function handleTimelineZoomChange(zoom) {
  state.timelineZoom = Number(zoom) || 1;
  logInfo(`时间轴缩放：x${state.timelineZoom.toFixed(2)}`);
}

function findKeysByRange(start, end) {
  return state.tokens
    .filter((token) => rangeOverlap(token.start, token.end, start, end))
    .map((token) => token.key);
}

// BEGIN-EDIT
function getMediaDuration() {
  if (dom.mediaPlayer && Number.isFinite(dom.mediaPlayer.duration) && dom.mediaPlayer.duration > 0) {
    return dom.mediaPlayer.duration;
  }
  if (Number.isFinite(state.mediaDuration) && state.mediaDuration > 0) {
    return state.mediaDuration;
  }
  if (state.timelineController && typeof state.timelineController._effectiveDuration === "function") {
    const duration = state.timelineController._effectiveDuration();
    if (Number.isFinite(duration) && duration > 0) {
      return duration;
    }
  }
  return null;
}

function clampMediaTime(value) {
  const duration = getMediaDuration();
  if (!Number.isFinite(value)) {
    return 0;
  }
  if (!Number.isFinite(duration) || duration <= 0) {
    return Math.max(0, value);
  }
  return Math.min(Math.max(value, 0), duration);
}

function getSelectedTimelineRange() {
  if (state.selectedKeys.size) {
    let start = Number.POSITIVE_INFINITY;
    let end = 0;
    state.selectedKeys.forEach((key) => {
      const token = state.tokenMap.get(key);
      if (!token) return;
      if (Number.isFinite(token.start)) {
        start = Math.min(start, token.start);
      }
      if (Number.isFinite(token.end)) {
        end = Math.max(end, token.end);
      }
    });
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) {
      return { start, end };
    }
  }
  if (state.timelinePendingRange && state.timelinePendingRangeTimestamp) {
    const elapsed = Date.now() - state.timelinePendingRangeTimestamp;
    if (Number.isFinite(elapsed) && elapsed >= 0 && elapsed < 5000) {
      return { ...state.timelinePendingRange };
    }
  }
  return null;
}

function shouldHandleTimelineSpace(event) {
  if (!state.timelineController || !dom.mediaPlayer) {
    return false;
  }
  const target = event.target;
  if (target) {
    const tag = target.tagName;
    if (target.isContentEditable) return false;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return false;
    if (target.closest?.("button,[role='button']")) return false;
  }
  if (getSelectedTimelineRange()) {
    return true;
  }
  const viewport = state.timelineController.viewport;
  const activeElement = document.activeElement;
  if (viewport) {
    if (viewport === target || viewport.contains?.(target)) return true;
    if (viewport === activeElement || viewport.contains?.(activeElement)) return true;
  }
  const recent = Date.now() - state.timelineLastInteractionAt;
  return Number.isFinite(recent) && recent >= 0 && recent < 2000;
}

function triggerTimelinePlayback() {
  if (!dom.mediaPlayer) return;
  const duration = getMediaDuration();
  if (!Number.isFinite(duration) || duration <= 0) {
    return;
  }
  const player = dom.mediaPlayer;
  // BEGIN-EDIT
  if (!player.paused) {
    state.segmentPlaybackEnd = null;
    state.timelinePendingRangeTimestamp = 0;
    state.timelineLastScrubTime = clampMediaTime(player.currentTime || 0);
    state.timelineLastInteractionAt = Date.now();
    player.pause();
    return;
  }
  const selectedRange = getSelectedTimelineRange();
  if (selectedRange) {
    const start = clampMediaTime(selectedRange.start);
    const end = clampMediaTime(selectedRange.end);
    if (end - start < 0.05) {
      player.currentTime = start;
      state.segmentPlaybackEnd = null;
    } else {
      player.currentTime = start;
      state.segmentPlaybackEnd = end;
    }
    state.timelineLastScrubTime = start;
    state.timelineLastInteractionAt = Date.now();
    state.timelinePendingRange = { start, end };
    state.timelinePendingRangeTimestamp = Date.now();
  } else {
    const fallback = Number.isFinite(state.timelineLastScrubTime)
      ? state.timelineLastScrubTime
      : player.currentTime || 0;
    const time = clampMediaTime(fallback);
    player.currentTime = time;
    state.timelineLastScrubTime = time;
    state.timelineLastInteractionAt = Date.now();
    state.segmentPlaybackEnd = null;
    state.timelinePendingRangeTimestamp = 0;
  }
  const playPromise = player.play?.();
  if (playPromise && typeof playPromise.catch === "function") {
    playPromise.catch(() => {});
  }
}
// END-EDIT

// ---------------------------------------------------------------------------
// 文件导入、项目创建、任务轮询
// ---------------------------------------------------------------------------

function handleTranscriptFilePick() {
  if (!dom.createFileInput || !dom.createNameInput) return;
  const file = dom.createFileInput.files?.[0];
  if (!file) return;
  if (!dom.createNameInput.value.trim()) {
    const base = file.name.replace(/\.[^.]+$/, "");
    dom.createNameInput.value = base;
  }
}

async function handleCreateProject(event) {
  event.preventDefault();
  if (!dom.createFileInput) return;
  const file = dom.createFileInput.files?.[0];
  if (!file) {
    logError(new Error("请选择 transcript.json 文件"));
    return;
  }
  try {
    const text = await file.text();
    const transcript = JSON.parse(text);
    const name = dom.createNameInput?.value?.trim() || file.name.replace(/\.[^.]+$/, "");
    const payload = {
      name,
      transcript,
    };
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`创建项目失败: ${response.status}`);
    const data = await response.json();
    logInfo(`项目 #${data?.project?.id} 已创建`);
    dom.createForm?.reset();
    await refreshProjects();
    if (data?.project?.id) {
      await setCurrentProject(data.project.id);
    }
  } catch (error) {
    logError(error);
  }
}

async function handleUploadAndTranscribe(event) {
  event.preventDefault();
  if (!dom.mediaFileInput) return;
  const file = dom.mediaFileInput.files?.[0];
  if (!file) {
    logError(new Error("请先选择音视频文件"));
    return;
  }
  state.currentMediaName = file.name;
  try {
    if (state.currentTaskId) {
      const taskLabel = state.currentTaskType === "cut" ? "生成剪辑" : "转写";
      logWarn(`当前存在${taskLabel}任务，请等待完成或停止后再试。`);
      resetTranscribeButton();
      return;
    }
    if (dom.transcribeSubmitButton) {
      dom.transcribeSubmitButton.disabled = true;
    }
    setTranscribeButtonProgress(0);
    if (dom.taskStatus) {
      dom.taskStatus.textContent = "已提交转写任务，正在上传音视频…";
    }
    const uploadForm = new FormData();
    uploadForm.append("file", file);
    const uploadResponse = await fetch("/api/uploads", {
      method: "POST",
      body: uploadForm,
    });
    if (!uploadResponse.ok) throw new Error(`上传文件失败: ${uploadResponse.status}`);
    const uploadData = await uploadResponse.json();
    const mediaPath = uploadData.path;
    if (!mediaPath) throw new Error("服务器未返回媒体路径");

    // BEGIN-EDIT
    if (state.currentTaskId) {
      const taskLabel = state.currentTaskType === "cut" ? "生成剪辑" : "转写";
      logWarn(`当前正在${taskLabel}，请先等待任务完成或取消导入。`);
      return;
    }
    const engine = dom.engineSelect?.value || "paraformer";
    const model = dom.modelSelect?.value || "paraformer-zh";
    // END-EDIT
    const device = dom.deviceSelect?.value || "auto";
    const taskPayload = {
      media_path: mediaPath,
      engine,
      model,
      device,
    };

    const taskResponse = await fetch("/api/tasks/transcribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(taskPayload),
    });
    if (!taskResponse.ok) throw new Error(`提交转写任务失败: ${taskResponse.status}`);
    const taskData = await taskResponse.json();
    state.currentTaskId = taskData.task_id;
    state.currentTaskType = "transcribe";
    startTaskPolling();
    dom.taskStatus.textContent = "已提交转写任务，正在后台执行…";
    if (state.localPreviewUrl) {
      URL.revokeObjectURL(state.localPreviewUrl);
    }
    state.localPreviewUrl = URL.createObjectURL(file);
    updateMediaSource({ previewOnly: true });
    logInfo("文件已上传并开始转写任务");
  } catch (error) {
    logError(error);
  }
}

function handleEngineChange() {
  if (!dom.engineSelect || !dom.modelSelect) return;
  // BEGIN-EDIT
  const engine = dom.engineSelect.value || "paraformer";
  const options = ENGINE_MODEL_OPTIONS[engine] || ENGINE_MODEL_OPTIONS.paraformer;
  // END-EDIT
  dom.modelSelect.innerHTML = "";
  options.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.value;
    option.textContent = item.label;
    dom.modelSelect.appendChild(option);
  });
}

function startTaskPolling() {
  stopTaskPolling();
  const poll = async () => {
    if (!state.currentTaskId) return;
    try {
      const response = await fetch(`/api/tasks/${state.currentTaskId}`);
      if (!response.ok) throw new Error(`查询任务失败: ${response.status}`);
      const data = await response.json();
      handleTaskUpdate(data);
      if (data.status === "completed" || data.status === "failed") {
        stopTaskPolling();
      } else {
        state.taskPollingTimer = setTimeout(poll, POLL_INTERVAL_MS);
      }
    } catch (error) {
      logError(error);
      if (state.currentTaskType === "cut") {
        resetCutButton();
      }
      if (state.currentTaskType === "transcribe") {
        resetTranscribeButton();
      }
      stopTaskPolling();
    }
  };
  poll();
}

function stopTaskPolling() {
  if (state.taskPollingTimer) {
    clearTimeout(state.taskPollingTimer);
    state.taskPollingTimer = null;
  }
}

async function handleTaskUpdate(data) {
  const type = state.currentTaskType;
  const status = data.status;
  if (type === "transcribe") {
    dom.taskStatus.textContent = `${data.message || status}`;
    const hasProgress = typeof data.progress === "number" && Number.isFinite(data.progress);
    if (status === "completed" || status === "failed") {
      if (status === "completed" && hasProgress) {
        setTranscribeButtonProgress(data.progress);
        setTimeout(() => resetTranscribeButton(), 300);
      } else {
        resetTranscribeButton();
      }
    } else {
      if (dom.transcribeSubmitButton) {
        dom.transcribeSubmitButton.disabled = true;
      }
      if (hasProgress) {
        setTranscribeButtonProgress(data.progress);
      }
    }
  } else if (type === "cut") {
    dom.cutStatus.textContent = `${data.message || status}`;
    const hasProgress = typeof data.progress === "number" && Number.isFinite(data.progress);
    if (status === "completed" || status === "failed") {
      resetCutButton();
    } else {
      if (dom.cutSubmitButton) {
        dom.cutSubmitButton.disabled = true;
      }
      setCutButtonProgress(hasProgress ? data.progress : null);
    }
  }
  if (status === "completed" && type === "transcribe") {
    await handleTranscribeCompleted(data);
    dom.taskStatus.textContent = "转写完成";
  }
  if (status === "completed" && type === "cut") {
    resetCutButton();
    dom.cutStatus.textContent = "剪辑完成";
    logInfo("剪辑任务已完成");
  }
  if (status === "failed") {
    logError(new Error(data.message || "后台任务失败"));
    if (type === "transcribe") {
      dom.taskStatus.textContent = data.message || "转写任务失败";
    } else if (type === "cut") {
      resetCutButton();
      dom.cutStatus.textContent = data.message || "剪辑任务失败";
    }
  }
  if (status === "completed" || status === "failed") {
    state.currentTaskId = null;
    state.currentTaskType = null;
  }
}

async function handleTranscribeCompleted(data) {
  const transcriptPayload = data.result?.transcript;
  const mediaPath = data.result?.media_path;
  if (!transcriptPayload) {
    logError(new Error("未收到转写结果"));
    return;
  }
  try {
    const response = await fetch("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name: state.currentMediaName || "自动转写项目",
        transcript: transcriptPayload,
        metadata: {
          media_path: mediaPath,
          media_name: state.currentMediaName || "",
        },
      }),
    });
    if (!response.ok) throw new Error(`创建项目失败: ${response.status}`);
    const projectData = await response.json();
    logInfo(`转写完成，项目 #${projectData?.project?.id} 已创建`);
    await refreshProjects();
    if (projectData?.project?.id) {
      await setCurrentProject(projectData.project.id);
    }
  } catch (error) {
    logError(error);
  }
}

function handleMediaSelection(event) {
  const file = event.target?.files?.[0];
  resetWorkspaceState();
  if (!file) {
    logInfo("未选择媒体文件，已清空当前场景");
    return;
  }
  state.currentMediaName = file.name;
  state.localPreviewUrl = URL.createObjectURL(file);
  updateMediaSource({ previewOnly: true });
  logInfo(`已选择媒体文件：${state.currentMediaName}`);
}

function updateMediaSource(options = {}) {
  if (!dom.mediaPlayer) return;
  const { previewOnly = false } = options;
  let source = "";
  if (!previewOnly && state.currentMediaPath) {
    if (state.localPreviewUrl) {
      URL.revokeObjectURL(state.localPreviewUrl);
      state.localPreviewUrl = null;
    }
    source = `/api/projects/${state.currentProjectId}/media`;
  } else if (state.localPreviewUrl) {
    source = state.localPreviewUrl;
  }
  if (source) {
    if (dom.mediaPlayer.src !== source) {
      dom.mediaPlayer.src = source;
      dom.mediaPlayer.load();
    }
  } else {
    dom.mediaPlayer.removeAttribute("src");
    dom.mediaPlayer.load();
  }
}

function handleMediaTimeUpdate() {
  if (!state.timelineController || !dom.mediaPlayer) return;
  // BEGIN-EDIT
  if (Number.isFinite(state.segmentPlaybackEnd)) {
    const current = dom.mediaPlayer.currentTime || 0;
    if (current >= state.segmentPlaybackEnd - 0.02) {
      dom.mediaPlayer.pause();
      dom.mediaPlayer.currentTime = state.segmentPlaybackEnd;
      state.segmentPlaybackEnd = null;
    }
  }
  // END-EDIT
  state.timelineController.updatePlayhead(dom.mediaPlayer.currentTime || 0);
}

// ---------------------------------------------------------------------------
// 保存 / 导出删除计划
// ---------------------------------------------------------------------------

async function handleSaveSelection() {
  if (!state.currentProjectId) {
    logError(new Error("暂无选中的项目"));
    return;
  }
  try {
    const deleteRanges = buildDeleteRangesFromDeletedKeys();
    await persistSelection(deleteRanges);
    logInfo("删除计划已保存");
  } catch (error) {
    logError(error);
  }
}

function handleExportSelection() {
  const deleteRanges = buildDeleteRangesFromDeletedKeys();
  const blob = new Blob([JSON.stringify({ delete_ranges: deleteRanges }, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `selection_${state.currentProjectId || "export"}.json`;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
  logInfo("删除计划已导出");
}

// ---------------------------------------------------------------------------
// 剪辑任务
// ---------------------------------------------------------------------------

function getTranscribeButtonBaseLabel() {
  if (!dom.transcribeSubmitButton) {
    return state.transcribeButtonDefaultLabel || "转写";
  }
  const defaultLabel =
    dom.transcribeSubmitButton.dataset?.defaultLabel ||
    dom.transcribeSubmitButton.textContent?.trim() ||
    state.transcribeButtonDefaultLabel ||
    "转写";
  if (dom.transcribeSubmitButton.dataset) {
    dom.transcribeSubmitButton.dataset.defaultLabel = defaultLabel;
  }
  state.transcribeButtonDefaultLabel = defaultLabel;
  return defaultLabel;
}

function setTranscribeButtonProgress(progress) {
  if (!dom.transcribeSubmitButton) return;
  const baseLabel = getTranscribeButtonBaseLabel();
  if (!Number.isFinite(progress)) {
    return;
  }
  const clamped = Math.min(1, Math.max(0, progress));
  const percent = Math.round(clamped * 100);
  dom.transcribeSubmitButton.textContent = `${baseLabel} ${percent}%`;
}

function resetTranscribeButton() {
  if (!dom.transcribeSubmitButton) return;
  dom.transcribeSubmitButton.disabled = false;
  dom.transcribeSubmitButton.textContent = getTranscribeButtonBaseLabel();
}

function getCutButtonBaseLabel() {
  if (!dom.cutSubmitButton) {
    return state.cutButtonDefaultLabel || "生成剪辑";
  }
  const defaultLabel =
    dom.cutSubmitButton.dataset?.defaultLabel ||
    dom.cutSubmitButton.textContent?.trim() ||
    state.cutButtonDefaultLabel ||
    "生成剪辑";
  if (dom.cutSubmitButton.dataset) {
    dom.cutSubmitButton.dataset.defaultLabel = defaultLabel;
  }
  state.cutButtonDefaultLabel = defaultLabel;
  return defaultLabel;
}

function setCutButtonProgress(progress) {
  if (!dom.cutSubmitButton) return;
  const baseLabel = getCutButtonBaseLabel();
  if (!Number.isFinite(progress)) {
    return;
  }
  const clamped = Math.min(1, Math.max(0, progress));
  const percent = Math.round(clamped * 100);
  dom.cutSubmitButton.textContent = `${baseLabel} ${percent}%`;
}

function resetCutButton() {
  if (!dom.cutSubmitButton) return;
  dom.cutSubmitButton.disabled = false;
  dom.cutSubmitButton.textContent = getCutButtonBaseLabel();
}

async function handleCutSubmit(event) {
  event.preventDefault();
  if (!state.currentProjectId) {
    logError(new Error("请先选择项目"));
    return;
  }
  if (!state.currentMediaPath) {
    logError(new Error("项目未关联媒体文件"));
    return;
  }
  try {
    const deleteRanges = buildDeleteRangesFromDeletedKeys();
    if (!deleteRanges.length) {
      logInfo("当前没有删除区间，建议先进行编辑");
    }
    await persistSelection(deleteRanges);
    const payload = {
      project_id: state.currentProjectId,
      input_path: state.currentMediaPath,
      output_name: dom.cutOutputName?.value || "",
      reencode: dom.cutReencode?.value || "nvenc",
      snap_zero_cross: (dom.cutSnapZero?.value ?? "true") !== "false",
      xfade_ms: Number(dom.cutXfadeMs?.value ?? 0),
      chunk_size: Math.max(1, Number(dom.cutChunkSize?.value ?? 20)),
    };
    const response = await fetch("/api/tasks/cut", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!response.ok) throw new Error(`提交剪辑任务失败: ${response.status}`);
    const data = await response.json();
    state.currentTaskId = data.task_id;
    state.currentTaskType = "cut";
    startTaskPolling();
    dom.cutStatus.textContent = "剪辑任务已提交，正在后台处理…";
    logInfo("剪辑任务已提交");
  } catch (error) {
    logError(error);
  }
}

async function handleExportSrtOnly() {
  if (!state.currentProjectId) {
    logError(new Error("请先选择项目"));
    return;
  }
  try {
    if (dom.exportSrtButton) {
      dom.exportSrtButton.disabled = true;
    }
    if (dom.cutStatus) {
      dom.cutStatus.textContent = "正在导出字幕...";
    }
    const response = await fetch(`/api/projects/${state.currentProjectId}/export/srt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output_name: dom.cutOutputName?.value || "" }),
    });
    if (!response.ok) {
      let message = `导出 SRT 失败 (${response.status})`;
      try {
        const errorData = await response.json();
        if (errorData?.error) {
          message = errorData.error;
        }
      } catch (error) {
        /* ignore json parse error */
      }
      throw new Error(message);
    }
    const data = await response.json();
    logInfo(`字幕已导出：${data.file_name || data.output_path}`);
    if (dom.cutStatus) {
      dom.cutStatus.textContent = "字幕导出完成";
      setTimeout(() => {
        if (dom.cutStatus?.textContent === "字幕导出完成") {
          dom.cutStatus.textContent = "";
        }
      }, 2000);
    }
  } catch (error) {
    logError(error instanceof Error ? error : new Error(String(error)));
    if (dom.cutStatus) {
      dom.cutStatus.textContent = "字幕导出失败";
    }
  } finally {
    if (dom.exportSrtButton) {
      dom.exportSrtButton.disabled = false;
    }
  }
}

// ---------------------------------------------------------------------------
// 波形与音频解码
// ---------------------------------------------------------------------------

async function refreshTimelineWaveform() {
  if (!state.timelineController) return;
  if (state.currentProjectId) {
    prepareTimelineWaveform({
      type: "project",
      projectId: state.currentProjectId,
      version: state.transcriptVersion ?? null,
    });
    return;
  }
  state.waveformSourceKey = null;
  state.timelineWaveform = null;
  state.timelineController.setWaveform(null);
  state.timelineController.setStatus("等待上传生成波形预览");
}

async function prepareTimelineWaveform(request) {
  if (!state.timelineController) return;
  if (!request) {
    refreshTimelineWaveform();
    return;
  }
  const taskId = ++state.waveformTaskId;
  try {
    let key = "";
    let arrayBuffer = null;
    if (request.type === "file" && request.source) {
      const file = request.source;
      if (!(file instanceof File)) {
        throw new Error("音频文件无效");
      }
      key = getFileWaveformKey(file);
      const cached = state.waveformCache.get(key);
      if (cached) {
        applyWaveformResult(key, cached);
        return;
      }
      state.timelineController.showWaveformLoading();
      arrayBuffer = await file.arrayBuffer();
    } else if (request.type === "project" && Number.isInteger(request.projectId)) {
      const projectId = request.projectId;
      let versionValue = Number.isFinite(request.version) ? Number(request.version) : null;
      if (versionValue !== null && versionValue <= 0) {
        versionValue = null;
      }
      key = getProjectWaveformKey(projectId, versionValue);
      const cached = state.waveformCache.get(key);
      if (cached) {
        applyWaveformResult(key, cached);
        return;
      }
      state.timelineController.showWaveformLoading();
      const params = new URLSearchParams();
      if (versionValue !== null) {
        params.set("version", String(versionValue));
      }
      const query = params.toString();
      const response = await fetch(
        `/api/projects/${projectId}/waveform${query ? `?${query}` : ""}`,
      );
      if (!response.ok) {
        throw new Error(`获取波形失败: ${response.status}`);
      }
      const data = await response.json();
      const payload = data?.waveform;
      if (!payload || !Array.isArray(payload.values)) {
        throw new Error("服务器未返回有效的波形数据");
      }
      if (state.waveformTaskId !== taskId) {
        return;
      }
      const durationValue = Number(payload.duration);
      const minValueRaw = Number(payload.min);
      const maxValueRaw = Number(payload.max);
      const waveformData = {
        values: Float32Array.from(payload.values),
        duration: Number.isFinite(durationValue) ? durationValue : 0,
        min: Number.isFinite(minValueRaw) ? minValueRaw : 0,
        max: Number.isFinite(maxValueRaw) ? maxValueRaw : 1,
      };
      state.waveformCache.set(key, waveformData);
      while (state.waveformCache.size > 8) {
        const oldestKey = state.waveformCache.keys().next().value;
        state.waveformCache.delete(oldestKey);
      }
      applyWaveformResult(key, waveformData);
      return;
    } else if (request.type === "url" && request.source) {
      const url = request.source;
      key = getUrlWaveformKey(url);
      const cached = state.waveformCache.get(key);
      if (cached) {
        applyWaveformResult(key, cached);
        return;
      }
      state.timelineController.showWaveformLoading();
      const response = await fetch(url);
      if (!response.ok) {
        throw new Error(`波形请求失败: ${response.status}`);
      }
      arrayBuffer = await response.arrayBuffer();
    } else {
      state.timelineController.setWaveform(null);
      state.timelineController.setStatus("请导入音视频以查看波形");
      return;
    }

    if (state.waveformTaskId !== taskId) {
      return;
    }

    const audioBuffer = await decodeAudioBuffer(arrayBuffer);
    if (!audioBuffer) {
      throw new Error("音频解码失败");
    }
    const values = buildWaveformValues(audioBuffer);
    if (state.waveformTaskId !== taskId) {
      return;
    }
    const minValue = values.length ? Math.min(...values) : 0;
    const maxValue = values.length ? Math.max(...values) : 0;
    const waveformData = {
      values: Float32Array.from(values),
      duration: audioBuffer.duration,
      min: minValue,
      max: maxValue,
    };
    state.waveformCache.set(key, waveformData);
    while (state.waveformCache.size > 8) {
      const oldestKey = state.waveformCache.keys().next().value;
      state.waveformCache.delete(oldestKey);
    }
    applyWaveformResult(key, waveformData);
  } catch (error) {
    if (state.waveformTaskId === taskId) {
      state.timelineController.setWaveform(null);
      state.timelineController.setStatus("波形加载失败");
      state.timelineWaveform = null;
      state.waveformSourceKey = null;
    }
    logError(error);
  }
}
function applyWaveformResult(key, payload) {
  if (!state.timelineController || !payload || !payload.values) {
    return;
  }
  state.waveformSourceKey = key;
  // BEGIN-EDIT
  const fallbackDuration = Number.isFinite(payload.duration) ? payload.duration : (state.mediaDuration ?? 0);
  const normalizedDuration = Math.max(0, fallbackDuration);
  const minValue = Number.isFinite(payload.min) ? payload.min : 0;
  const maxValue = Number.isFinite(payload.max) ? payload.max : 1;
  const valuesArray = Array.isArray(payload.values)
    ? payload.values.slice()
    : Array.from(payload.values || []);
  const waveformValues = Float32Array.from(valuesArray);
  if (typeof console !== "undefined") {
    console.debug("timeline waveform loaded", key, waveformValues.length, normalizedDuration);
  }
  state.timelineWaveform = { values: waveformValues, duration: normalizedDuration, min: minValue, max: maxValue };
  state.mediaDuration = normalizedDuration;
  state.timelineController.setMediaDuration(normalizedDuration);
  state.timelineController.setWaveform({
    values: waveformValues,
    duration: normalizedDuration,
    min: minValue,
    max: maxValue,
  });
  // END-EDIT
  state.timelineController.setStatus("");
}




function buildWaveformValues(audioBuffer, targetPoints = 2000) {
  const duration = Math.max(0.1, audioBuffer.duration || 0);
  const totalSamples = audioBuffer.length || 0;
  const channelCount = Math.max(1, audioBuffer.numberOfChannels);
  const channelData = Array.from({ length: channelCount }, (_, index) => audioBuffer.getChannelData(index));

  const basePointTarget = Math.min(targetPoints, Math.max(500, Math.ceil(duration * 60)));
  const cappedPoints = Math.max(256, basePointTarget);
  const sampleCount = Math.max(1, Math.min(totalSamples || 1, cappedPoints));
  const values = new Float32Array(sampleCount);
  if (!totalSamples || !channelData.length) {
    return values;
  }

  const stride = Math.max(1, Math.floor(totalSamples / sampleCount));
  const decimation = Math.max(1, Math.floor(stride / 64));

  for (let i = 0; i < sampleCount; i += 1) {
    const startIndex = i * stride;
    const endIndex = i === sampleCount - 1 ? totalSamples : Math.min(totalSamples, startIndex + stride);
    let peak = 0;
    for (let sampleIndex = startIndex; sampleIndex < endIndex; sampleIndex += decimation) {
      for (let channel = 0; channel < channelCount; channel += 1) {
        const value = Math.abs(channelData[channel][sampleIndex] || 0);
        if (value > peak) {
          peak = value;
        }
      }
    }
    values[i] = Math.min(1, peak);
  }
  return values;
}


function getFileWaveformKey(file) {
  return `file:${file.name}:${file.size}:${file.lastModified}`;
}

function getProjectWaveformKey(projectId, version) {
  const suffix = Number.isFinite(version) ? version : "latest";
  return `project:${projectId}:${suffix}`;
}

function getUrlWaveformKey(url) {
  return `url:${url}`;
}

async function decodeAudioBuffer(arrayBuffer) {
  if (!arrayBuffer || !arrayBuffer.byteLength) return null;
  const audioContext = new (window.AudioContext || window.webkitAudioContext)();
  try {
    const buffer = await audioContext.decodeAudioData(arrayBuffer.slice(0));
    audioContext.close();
    return buffer;
  } catch (error) {
    console.warn("音频解码失败", error);
    audioContext.close();
    return null;
  }
}

// ---------------------------------------------------------------------------
// 日志与提示
// ---------------------------------------------------------------------------

function logInfo(message) {
  appendLogEntry("info", message instanceof Error ? message.message : String(message));
}

function logWarn(message) {
  appendLogEntry("warn", message instanceof Error ? message.message : String(message));
}

function logError(error) {
  const message = error instanceof Error ? error.message : String(error);
  appendLogEntry("error", message);
  console.error(error);
}

function appendLogEntry(level, message) {
  if (!dom.log) return;
  const entry = document.createElement("div");
  entry.className = `log-entry log-${level}`;
  entry.textContent = `[${formatTimestamp(new Date())}] ${message}`;
  dom.log.appendChild(entry);
  dom.log.scrollTop = dom.log.scrollHeight;
}

function toggleLogDrawer(force) {
  if (!dom.logDrawer) return;
  const open = force == null ? !dom.logDrawer.classList.contains("open") : Boolean(force);
  dom.logDrawer.classList.toggle("open", open);
  dom.logDrawer.setAttribute("aria-hidden", open ? "false" : "true");
}

function formatTimestamp(date) {
  return `${date.getHours().toString().padStart(2, "0")}:${date.getMinutes().toString().padStart(2, "0")}:${date
    .getSeconds()
    .toString()
    .padStart(2, "0")}`;
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const totalSeconds = Math.max(0, Math.round(seconds));
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) {
    return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  }
  return `${m}:${s.toString().padStart(2, "0")}`;
}















  if (dom.transcribeSubmitButton) {
    const label =
      dom.transcribeSubmitButton.textContent?.trim() ||
      state.transcribeButtonDefaultLabel ||
      "转写";
    if (dom.transcribeSubmitButton.dataset) {
      dom.transcribeSubmitButton.dataset.defaultLabel = label;
    }
    state.transcribeButtonDefaultLabel = label;
  }
