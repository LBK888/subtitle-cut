const MIN_PIXELS_PER_SECOND = 40;
const MAX_CANVAS_WIDTH = 32768;
const MAX_PIXELS_PER_SECOND = 600;
const MIN_ZOOM = 0.25;
const MAX_ZOOM = 6;

export class TimelineController {
  constructor(options) {
    const {
      container,
      onScrub,
      onToggleRange,
      onHoverKeys,
      onZoomChange,
      isRangeSelected,
      getAnchor,
      onFocusRange,
    } = options || {};
    if (!container) {
      throw new Error("缺少时间轴容器节点");
    }
    this.container = container;
    this.onScrub = typeof onScrub === "function" ? onScrub : null;
    this.onToggleRange = typeof onToggleRange === "function" ? onToggleRange : null;
    this.onHoverKeys = typeof onHoverKeys === "function" ? onHoverKeys : null;
    this.onZoomChange = typeof onZoomChange === "function" ? onZoomChange : null;
    this.isRangeSelected = typeof isRangeSelected === "function" ? isRangeSelected : (() => false);
    this.getAnchor = typeof getAnchor === "function" ? getAnchor : (() => null);
    this.onFocusRange = typeof onFocusRange === "function" ? onFocusRange : null;

    this.tokens = [];
    this.boundaries = [];
    this.deleteRanges = [];
    this.duration = 0;
    this.mediaDuration = null;
    this.basePixelsPerSecond = 80;
    this.zoom = 1;
    this.pixelsPerSecond = this.basePixelsPerSecond * this.zoom;
    this.waveform = null;
    this.waveformLoading = false;
    this.statusMessage = "";
    this.playheadTime = 0;
    this.mediaElement = null;
    this.scrubbing = false;
    this.hoverToken = null;
    this.previewRange = null;

    this._buildDom();
    this._bindEvents();
    this._updateScale();
    this.setStatus("等待转录数据");
  }

  destroy() {
    this._unbindEvents();
    if (this.mediaElement) {
      this._unhookMedia(this.mediaElement);
      this.mediaElement = null;
    }
  }

  bindMedia(mediaElement) {
    if (this.mediaElement === mediaElement) {
      return;
    }
    if (this.mediaElement) {
      this._unhookMedia(this.mediaElement);
    }
    this.mediaElement = mediaElement;
    if (mediaElement) {
      this._hookMedia(mediaElement);
      if (Number.isFinite(mediaElement.currentTime)) {
        this.updatePlayhead(mediaElement.currentTime);
      }
      if (Number.isFinite(mediaElement.duration)) {
        this.setMediaDuration(mediaElement.duration);
      }
    }
  }

  setData(payload) {
    if (!payload) {
      this.tokens = [];
      this.boundaries = [];
      this.duration = 0;
      this.setStatus("等待转录数据");
      this._updateScale();
      return;
    }
    const { tokens, boundaries, duration } = payload;
    this.tokens = Array.isArray(tokens) ? tokens.slice() : [];
    this.boundaries = Array.isArray(boundaries) ? boundaries.slice() : [];
    this.duration = Number.isFinite(duration) ? Math.max(duration, 0) : 0;
    if (!this.duration && !this.mediaDuration) {
      this.setStatus("等待转录数据");
    } else if (!this.waveformLoading) {
      this.setStatus("");
    }
    this._ensureBaseScale();
    this._updateScale();
  }

  setDeleteRanges(ranges) {
    this.deleteRanges = Array.isArray(ranges) ? ranges.map((item) => ({
      start: Number(item.start),
      end: Number(item.end),
    })).filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end) && item.end > item.start) : [];
    this._renderSelections();
  }

  setZoom(nextZoom, options = {}) {
    const desired = Number.isFinite(Number(nextZoom)) ? Number(nextZoom) : 1;
    let zoomValue = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, desired));

    const effectiveDuration = this._effectiveDuration();
    if (effectiveDuration && this.basePixelsPerSecond > 0) {
      const maxZoomByCanvas = MAX_CANVAS_WIDTH / (effectiveDuration * this.basePixelsPerSecond);
      if (Number.isFinite(maxZoomByCanvas) && maxZoomByCanvas > 0) {
        const safeMax = Math.max(0.05, Math.min(MAX_ZOOM, maxZoomByCanvas));
        if (safeMax < MIN_ZOOM) {
          zoomValue = Math.max(safeMax, 0.05);
        } else {
          zoomValue = Math.min(zoomValue, safeMax);
        }
      }
    }

    if (Math.abs(zoomValue - this.zoom) < 0.001) {
      return this.zoom;
    }
    this.zoom = zoomValue;
    this.pixelsPerSecond = this.basePixelsPerSecond * this.zoom;
    this._updateScale();
    if (!options.silent && this.onZoomChange) {
      this.onZoomChange(this.zoom);
    }
    return this.zoom;
  }

  getZoom() {
    return this.zoom;
  }

  autoFit(options = {}) {
    const duration = this._effectiveDuration();
    if (!duration) {
      return;
    }
    const viewportWidth = this.viewport.clientWidth || this.container.clientWidth || 960;
    if (viewportWidth <= 0) {
      return;
    }
    const target = viewportWidth / duration;
    const maxBase = Math.max(0.1, MAX_CANVAS_WIDTH / duration);
    let base = Math.min(MAX_PIXELS_PER_SECOND, Math.max(MIN_PIXELS_PER_SECOND, target));
    if (base > maxBase) {
      base = maxBase < MIN_PIXELS_PER_SECOND ? Math.max(0.1, maxBase) : Math.max(MIN_PIXELS_PER_SECOND, maxBase);
    }
    this.basePixelsPerSecond = base;
    this.setZoom(1, { silent: options.silent });
  }

  setWaveform(waveform) {
    if (!waveform || !waveform.values || !waveform.values.length) {
      this.waveform = null;
      this.waveformLoading = false;
      this.setStatus(this.duration ? "" : "等待转录数据");
      this._draw();
      return;
    }
    // BEGIN-EDIT
    this.waveform = {
      values: waveform.values,
      min: waveform.min ?? -1,
      max: waveform.max ?? 1,
      duration: Number.isFinite(waveform.duration) ? Math.max(0, waveform.duration) : null,
    };
    // END-EDIT
    this.waveformLoading = false;
    this.setStatus("");
    this._draw();
  }

  showWaveformLoading(message = "波形加载中...") {
    this.waveformLoading = true;
    this.setStatus(message);
  }

  setStatus(message) {
    if (!this.status) return;
    const text = String(message || "").trim();
    this.status.textContent = text;
    this.status.classList.toggle("visible", Boolean(text));
  }

  updatePlayhead(time) {
    const clamped = Math.max(0, Math.min(Number(time) || 0, this._effectiveDuration() || 0));
    this.playheadTime = clamped;
    if (!this.playhead) return;
    const left = clamped * this.pixelsPerSecond;
    this.playhead.style.left = `${left}px`;
    this.playhead.classList.toggle("visible", Number.isFinite(clamped));
  }

  setMediaDuration(duration) {
    if (!Number.isFinite(duration) || duration <= 0) {
      return;
    }
    this.mediaDuration = duration;
    this._ensureBaseScale();
    this._updateScale();
  }

  clearPreview() {
    this.previewRange = null;
    this._renderPreview();
    if (this.onHoverKeys) {
      this.onHoverKeys([], false);
    }
  }

  setPreview(range, token, triggerHover = true) {
    if (!range) {
      this.clearPreview();
      return;
    }
    this.previewRange = {
      start: range.start,
      end: range.end,
    };
    this._renderPreview();
    if (triggerHover && this.onHoverKeys) {
      let keys = Array.isArray(token?.keys) ? token.keys : [];
      if ((!keys || !keys.length) && token && token.key != null) {
        keys = [token.key];
      }
      this.onHoverKeys(keys, true);
    }
  }

  focusRange(range, token, options = {}) {
    if (!range) {
      return;
    }
    const start = Number(range.start);
    if (!Number.isFinite(start)) {
      return;
    }
    const normalizedStart = Math.max(0, start);
    const endValue = Number(range.end);
    const normalizedEnd = Number.isFinite(endValue) ? Math.max(normalizedStart, endValue) : normalizedStart;
    const previewRange = { start: normalizedStart, end: normalizedEnd };
    const shouldPreview = options.preview !== false;
    if (shouldPreview) {
      this.setPreview(previewRange, token, false);
    }
    if (options.seek !== false) {
      this._seekTo(normalizedStart);
    } else {
      this.updatePlayhead(normalizedStart);
    }
    this._scrollToTime(options.center === "start" ? normalizedStart : (normalizedStart + normalizedEnd) / 2, options.behavior);
  }

  handleResize() {
    this._ensureBaseScale();
    this._updateScale();
  }

  // ---------------------------------------------------------------------------
  // 内部实�?
  // ---------------------------------------------------------------------------
  _buildDom() {
    this.container.classList.add("timeline-ready");
    this.container.innerHTML = "";

    this.viewport = document.createElement("div");
    this.viewport.className = "timeline-viewport";
    // BEGIN-EDIT
    this.viewport.tabIndex = 0;
    this.viewport.setAttribute("aria-label", "时间轴");
    this.viewport.setAttribute("data-focus", "timeline");
    // END-EDIT

    this.content = document.createElement("div");
    this.content.className = "timeline-content";

    this.canvas = document.createElement("canvas");
    this.canvas.className = "timeline-waveform";
    this.ctx = this.canvas.getContext("2d", { alpha: false });

    this.selectionLayer = document.createElement("div");
    this.selectionLayer.className = "timeline-selection-layer";

    this.previewLayer = document.createElement("div");
    this.previewLayer.className = "timeline-preview-layer";

    this.playhead = document.createElement("div");
    this.playhead.className = "timeline-playhead";

    this.content.appendChild(this.canvas);
    this.content.appendChild(this.selectionLayer);
    this.content.appendChild(this.previewLayer);
    this.content.appendChild(this.playhead);
    this.viewport.appendChild(this.content);

    this.status = document.createElement("div");
    this.status.className = "timeline-status";

    this.container.appendChild(this.viewport);
    this.container.appendChild(this.status);
  }

  _bindEvents() {
    this._handlePointerDown = this._handlePointerDown.bind(this);
    this._handlePointerMove = this._handlePointerMove.bind(this);
    this._handlePointerUp = this._handlePointerUp.bind(this);
    this._handleWheel = this._handleWheel.bind(this);
    this._handleResize = this.handleResize.bind(this);
    this._handleDoubleClick = this._handleDoubleClick.bind(this);

    this.viewport.addEventListener("pointerdown", this._handlePointerDown);
    this.viewport.addEventListener("pointermove", this._handlePointerMove);
    this.viewport.addEventListener("pointerleave", () => this.clearPreview());
    this.viewport.addEventListener("pointerup", this._handlePointerUp);
    this.viewport.addEventListener("lostpointercapture", this._handlePointerUp);
    this.viewport.addEventListener("wheel", this._handleWheel, { passive: false });
    this.viewport.addEventListener("dblclick", this._handleDoubleClick);
    window.addEventListener("resize", this._handleResize, { passive: true });
  }

  _unbindEvents() {
    this.viewport.removeEventListener("pointerdown", this._handlePointerDown);
    this.viewport.removeEventListener("pointermove", this._handlePointerMove);
    this.viewport.removeEventListener("pointerleave", () => this.clearPreview());
    this.viewport.removeEventListener("pointerup", this._handlePointerUp);
    this.viewport.removeEventListener("lostpointercapture", this._handlePointerUp);
    this.viewport.removeEventListener("wheel", this._handleWheel);
    this.viewport.removeEventListener("dblclick", this._handleDoubleClick);
    window.removeEventListener("resize", this._handleResize);
  }

  _hookMedia(media) {
    this._mediaTimeUpdate = () => this.updatePlayhead(media.currentTime);
    this._mediaDurationChange = () => this.setMediaDuration(media.duration);
    media.addEventListener("timeupdate", this._mediaTimeUpdate);
    media.addEventListener("seeking", this._mediaTimeUpdate);
    media.addEventListener("seeked", this._mediaTimeUpdate);
    media.addEventListener("loadedmetadata", this._mediaDurationChange);
    media.addEventListener("durationchange", this._mediaDurationChange);
  }

  _unhookMedia(media) {
    if (!media) return;
    if (this._mediaTimeUpdate) {
      media.removeEventListener("timeupdate", this._mediaTimeUpdate);
      media.removeEventListener("seeking", this._mediaTimeUpdate);
      media.removeEventListener("seeked", this._mediaTimeUpdate);
    }
    if (this._mediaDurationChange) {
      media.removeEventListener("loadedmetadata", this._mediaDurationChange);
      media.removeEventListener("durationchange", this._mediaDurationChange);
    }
  }

  _effectiveDuration() {
    return Math.max(this.mediaDuration || 0, this.duration || 0);
  }

  _ensureBaseScale() {
    const effectiveDuration = this._effectiveDuration();
    if (!effectiveDuration) {
      const fallbackBase = Number.isFinite(this.basePixelsPerSecond) && this.basePixelsPerSecond > 0
        ? this.basePixelsPerSecond
        : MIN_PIXELS_PER_SECOND;
      this.basePixelsPerSecond = Math.max(fallbackBase, MIN_PIXELS_PER_SECOND);
      this.pixelsPerSecond = this.basePixelsPerSecond * this.zoom;
      return;
    }

    const viewportWidth = this.viewport.clientWidth || this.container.clientWidth || 960;
    let base = this.basePixelsPerSecond;
    if (!Number.isFinite(base) || base <= 0) {
      base = viewportWidth / effectiveDuration;
    }

    const maxAllowed = Math.max(0.1, MAX_CANVAS_WIDTH / effectiveDuration);
    base = Math.min(base, MAX_PIXELS_PER_SECOND);
    if (base > maxAllowed) {
      base = maxAllowed;
    }
    if (maxAllowed < MIN_PIXELS_PER_SECOND) {
      base = Math.max(0.1, Math.min(base, maxAllowed));
    } else {
      base = Math.max(base, MIN_PIXELS_PER_SECOND);
    }

    this.basePixelsPerSecond = base;
    this.pixelsPerSecond = this.basePixelsPerSecond * this.zoom;
  }

  _updateScale() {
    const effectiveDuration = this._effectiveDuration();
    this.pixelsPerSecond = this.basePixelsPerSecond * this.zoom;
    const viewportWidth = this.viewport.clientWidth || this.container.clientWidth || 0;
    const computedWidth = effectiveDuration ? Math.ceil(effectiveDuration * this.pixelsPerSecond) : 0;
    let width = Math.max(viewportWidth, computedWidth);

    if (width > MAX_CANVAS_WIDTH) {
      width = MAX_CANVAS_WIDTH;
      if (effectiveDuration) {
        this.pixelsPerSecond = width / effectiveDuration;
        const adjustedZoom = this.pixelsPerSecond / (this.basePixelsPerSecond || 1);
        if (Number.isFinite(adjustedZoom) && adjustedZoom > 0) {
          this.zoom = Math.min(MAX_ZOOM, Math.max(0.05, adjustedZoom));
        }
      }
    }

    this.content.style.width = `${Math.max(width, 0)}px`;
    const height = Math.max(120, this.viewport.clientHeight || this.container.clientHeight || 160);
    if (this.canvas.width !== Math.floor(width)) {
      this.canvas.width = Math.max(1, Math.floor(width));
    }
    if (this.canvas.height !== Math.floor(height)) {
      this.canvas.height = Math.floor(height);
    }
    this._draw();
    this._renderSelections();
    this._renderPreview();
    this.updatePlayhead(this.playheadTime);
  }

  _draw() {
    if (!this.ctx) return;
    const ctx = this.ctx;
    const width = this.canvas.width;
    const height = this.canvas.height;
    ctx.clearRect(0, 0, width, height);

    ctx.fillStyle = "#080a11";
    ctx.fillRect(0, 0, width, height);

    const mid = height / 2;

    if (this.tokens.length) {
      ctx.save();
      ctx.globalAlpha = 0.8;
      this.tokens.forEach((token) => {
        const start = token.start * this.pixelsPerSecond;
        let tokenWidth = (token.end - token.start) * this.pixelsPerSecond;
        if (!Number.isFinite(tokenWidth) || tokenWidth <= 1) {
          tokenWidth = 1;
        }
        ctx.fillStyle = token.type === "word"
          ? "rgba(59,130,246,0.15)"
          : "rgba(255,255,255,0.08)";
        ctx.fillRect(Math.floor(start), 0, Math.ceil(tokenWidth), height);
      });
      ctx.restore();
    } else {
      ctx.save();
      ctx.fillStyle = "rgba(255,255,255,0.04)";
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    }

    ctx.save();
    ctx.strokeStyle = "rgba(255,255,255,0.08)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(width, mid);
    ctx.stroke();
    ctx.restore();

    // BEGIN-EDIT
    const waveformDuration = Number.isFinite(this.waveform?.duration) ? Math.max(this.waveform.duration, 0) : 0;
    const effectiveDuration = Math.max(this._effectiveDuration() || 0, waveformDuration);
    const waveformWidth = effectiveDuration > 0 ? Math.round(effectiveDuration * this.pixelsPerSecond) : width;

    if (effectiveDuration > 0 && this.pixelsPerSecond > 0) {
      const stepCandidates = [0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
      const targetSpacing = 100;
      const secondsPerPixel = 1 / this.pixelsPerSecond;
      const minimumStep = targetSpacing * secondsPerPixel;
      let step = stepCandidates[stepCandidates.length - 1];
      for (let i = 0; i < stepCandidates.length; i += 1) {
        if (stepCandidates[i] >= minimumStep) {
          step = stepCandidates[i];
          break;
        }
      }
      const fractionDigits = step <= 0.1 ? 2 : step < 1 ? 1 : 0;
      const formatTime = (value) => {
        const total = Math.max(0, Number(value) || 0);
        let hours = Math.floor(total / 3600);
        let minutes = Math.floor((total % 3600) / 60);
        let seconds = total % 60;
        if (fractionDigits === 0) {
          seconds = Math.floor(seconds + 1e-6);
        } else {
          seconds = Number(seconds.toFixed(fractionDigits));
        }
        if (seconds >= 60) {
          seconds -= 60;
          minutes += 1;
        }
        if (minutes >= 60) {
          minutes -= 60;
          hours += 1;
        }
        const secondsText = fractionDigits === 0
          ? String(seconds).padStart(2, "0")
          : seconds.toFixed(fractionDigits).padStart(fractionDigits > 1 ? 5 : 4, "0");
        const minutesText = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
        return hours > 0 ? `${hours}:${minutesText}:${secondsText}` : `${minutesText}:${secondsText}`;
      };

      ctx.save();
      ctx.strokeStyle = "rgba(255,255,255,0.12)";
      ctx.fillStyle = "rgba(255,255,255,0.48)";
      ctx.font = "11px sans-serif";
      ctx.textBaseline = "top";
      ctx.lineWidth = 1;

      for (let marker = 0; marker <= effectiveDuration + step; marker += step) {
        const x = Math.round(marker * this.pixelsPerSecond) + 0.5;
        if (x > width) {
          break;
        }
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, 16);
        ctx.stroke();
        const label = formatTime(marker);
        if (x + 4 < width) {
          ctx.fillText(label, x + 4, 2);
        }
      }
      ctx.restore();
    }

    if (this.waveform?.values?.length) {
      const values = this.waveform.values;
      const length = values.length;
      const drawWidth = Math.max(1, Math.min(width, waveformWidth || width));
      ctx.save();
      ctx.strokeStyle = "rgba(126,196,255,0.95)";
      ctx.lineWidth = 1;
      for (let x = 0; x < drawWidth; x += 1) {
        const index = Math.floor((x / drawWidth) * length);
        const amp = Math.min(1, Math.abs(values[index] ?? 0));
        const amplitude = Math.max(1, amp * (height * 0.45));
        ctx.beginPath();
        ctx.moveTo(x + 0.5, mid - amplitude);
        ctx.lineTo(x + 0.5, mid + amplitude);
        ctx.stroke();
      }
      ctx.restore();
    }
    // END-EDIT
  }

  _renderSelections() {
    if (!this.selectionLayer) return;
    this.selectionLayer.innerHTML = "";
    if (!this.deleteRanges.length) {
      return;
    }
    this.deleteRanges.forEach((range) => {
      const node = document.createElement("div");
      node.className = "timeline-selection";
      const start = range.start * this.pixelsPerSecond;
      const width = Math.max((range.end - range.start) * this.pixelsPerSecond, 2);
      node.style.left = `${start}px`;
      node.style.width = `${width}px`;
      this.selectionLayer.appendChild(node);
    });
  }

  _renderPreview() {
    if (!this.previewLayer) return;
    this.previewLayer.innerHTML = "";
    if (!this.previewRange) {
      return;
    }
    const node = document.createElement("div");
    node.className = "timeline-preview";
    const start = this.previewRange.start * this.pixelsPerSecond;
    const width = Math.max((this.previewRange.end - this.previewRange.start) * this.pixelsPerSecond, 2);
    node.style.left = `${start}px`;
    node.style.width = `${width}px`;
    this.previewLayer.appendChild(node);
  }

  _isPointerOnHorizontalScrollbar(event) {
    if (!this.viewport || !event) {
      return false;
    }
    const scrollbarThickness = this.viewport.offsetHeight - this.viewport.clientHeight;
    if (scrollbarThickness <= 0 || !Number.isFinite(event.clientY)) {
      return false;
    }
    const rect = this.viewport.getBoundingClientRect();
    const offsetY = event.clientY - rect.top;
    return offsetY >= this.viewport.clientHeight;
  }

  _handlePointerDown(event) {
    if (event.pointerType === "touch") {
      return;
    }
    if (event.button !== 0) {
      return;
    }
    if (this._isPointerOnHorizontalScrollbar(event)) {
      return;
    }
    const position = this._eventPosition(event);
    if (!position) return;
    const { time } = position;

    if (event.ctrlKey || event.metaKey) {
      event.preventDefault();
      this._handleSelectionToggle(time, event);
      return;
    }

    this.scrubbing = true;
    // BEGIN-EDIT
    if (this.viewport && typeof this.viewport.focus === "function") {
      this.viewport.focus();
    }
    // END-EDIT
    this.viewport.setPointerCapture(event.pointerId);
    this._seekTo(time);
  }

  _handleDoubleClick(event) {
    if (!(event instanceof MouseEvent)) {
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    const position = this._eventPosition(event);
    if (!position) {
      return;
    }
    const token = this._findTokenAtTime(position.time) || this._nearestToken(position.time);
    if (!token) {
      return;
    }
    const range = { start: token.start, end: token.end };
    this.focusRange(range, token, { behavior: "smooth" });
    if (this.onFocusRange) {
      const keys = Array.isArray(token.keys) && token.keys.length ? token.keys : (token.key ? [token.key] : []);
      this.onFocusRange({ range, token, keys });
    }
  }

  _handlePointerMove(event) {
    if (event.pointerType === "touch") {
      return;
    }
    if (this._isPointerOnHorizontalScrollbar(event)) {
      this.clearPreview();
      return;
    }
    const position = this._eventPosition(event);
    if (!position) {
      this.clearPreview();
      return;
    }
    const { time } = position;

    if (this.scrubbing) {
      this._seekTo(time);
      return;
    }

    const token = this._findTokenAtTime(time);
    if (!token) {
      this.clearPreview();
      return;
    }
    if (!this.previewRange || this.previewRange.start !== token.start || this.previewRange.end !== token.end) {
      this.setPreview({ start: token.start, end: token.end }, token);
    }
  }

  _handlePointerUp(event) {
    if (event.pointerType === "touch") {
      return;
    }
    if (this.scrubbing) {
      this.scrubbing = false;
      if (event.pointerId != null) {
        try {
          this.viewport.releasePointerCapture(event.pointerId);
        } catch (_) {
          // ignore
        }
      }
    }
  }

  _handleSelectionToggle(time, event) {
    if (!this.tokens.length) {
      return;
    }
    const token = this._findTokenAtTime(time) || this._nearestToken(time);
    if (!token) {
      return;
    }
    const range = { start: token.start, end: token.end };
    const alreadySelected = this.isRangeSelected(range);
    const additive = event.shiftKey || !alreadySelected;
    this.setPreview(range, token, false);
    if (this.onToggleRange) {
      this.onToggleRange(range, {
        additive,
        shift: event.shiftKey,
        keys: token.keys || [],
      });
    }
  }

  _handleWheel(event) {
    if (!(event.ctrlKey || event.metaKey || event.shiftKey)) {
      return;
    }
    event.preventDefault();
    const delta = event.deltaY > 0 ? -0.2 : 0.2;
    const focus = this._eventPosition(event);
    const previousTime = focus ? focus.time : null;
    const beforeZoomPps = this.pixelsPerSecond;
    const newZoom = this.setZoom(this.zoom + delta);
    if (previousTime != null && newZoom) {
      const afterZoomPps = this.pixelsPerSecond;
      if (beforeZoomPps > 0 && afterZoomPps > 0) {
        const viewportRect = this.viewport.getBoundingClientRect();
        const pointerOffset = focus.clientX - viewportRect.left;
        const targetScroll = previousTime * afterZoomPps - pointerOffset;
        this.viewport.scrollLeft = Math.max(0, targetScroll);
      }
    }
  }

  _seekTo(time) {
    const clamped = Math.max(0, Math.min(time, this._effectiveDuration() || 0));
    this.updatePlayhead(clamped);
    if (this.onScrub) {
      this.onScrub(clamped);
    }
  }

  _scrollToTime(time, behavior = "smooth") {
    if (!this.viewport || !Number.isFinite(time) || this.pixelsPerSecond <= 0) {
      return;
    }
    const viewportWidth = this.viewport.clientWidth || this.viewport.offsetWidth || 0;
    const targetX = Math.max(0, time * this.pixelsPerSecond);
    const offset = viewportWidth > 0 ? Math.max(0, targetX - viewportWidth / 2) : targetX;
    if (typeof this.viewport.scrollTo === "function") {
      try {
        this.viewport.scrollTo({ left: offset, behavior });
        return;
      } catch (error) {
        // fall back to direct assignment
      }
    }
    this.viewport.scrollLeft = offset;
  }

  _eventPosition(event) {
    const viewportRect = this.viewport.getBoundingClientRect();
    const clientX = event.clientX;
    if (Number.isNaN(clientX)) {
      return null;
    }
    const offsetX = clientX - viewportRect.left;
    const scroll = this.viewport.scrollLeft || 0;
    const x = scroll + offsetX;
    if (this.pixelsPerSecond <= 0) {
      return null;
    }
    const time = x / this.pixelsPerSecond;
    return {
      x,
      time: Math.max(0, time),
      clientX,
    };
  }

  _findTokenAtTime(time) {
    if (!this.tokens.length) return null;
    let left = 0;
    let right = this.tokens.length - 1;
    while (left <= right) {
      const mid = (left + right) >> 1;
      const token = this.tokens[mid];
      if (time < token.start) {
        right = mid - 1;
      } else if (time >= token.end) {
        left = mid + 1;
      } else {
        return token;
      }
    }
    return null;
  }

  _nearestToken(time) {
    if (!this.tokens.length) return null;
    let closest = this.tokens[0];
    let minDiff = Math.abs((closest.start + closest.end) / 2 - time);
    for (let i = 1; i < this.tokens.length; i += 1) {
      const token = this.tokens[i];
      const center = (token.start + token.end) / 2;
      const diff = Math.abs(center - time);
      if (diff < minDiff) {
        minDiff = diff;
        closest = token;
      }
    }
    return closest;
  }
}
