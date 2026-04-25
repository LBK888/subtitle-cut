export const SILENCE_MIN_DURATION = 0.16;
const PUNCTUATION_MIN_DURATION = 0.35;
const MIN_SEGMENT_DURATION = 0.01;
const GAP_EPSILON = 1e-4;
export const SILENCE_COMMA_THRESHOLD = 0.55;
export const SILENCE_SENTENCE_THRESHOLD = 1.1;
export const SILENCE_PLACEHOLDER_THRESHOLD = 1.6;
const INNER_PUNCTUATION_MIN_DURATION = 0.6;

const CH_COMMA = "\uFF0C";
const CH_PERIOD = "\u3002";
const CH_COLON = "\uFF1A";
const CH_SEMICOLON = "\uFF1B";
const CH_EXCLAMATION = "\uFF01";
const CH_QUESTION = "\uFF1F";

const PUNCTUATION_CHAR_SET = new Set(["\uFF0C", "\u3002", "\uFF01", "\uFF1F", "\uFF1B", "\uFF1A", ",", ".", "!", "?", ";", ":", "\u3001"]);
const ASCII_WORD_REGEX = /^[A-Za-z0-9]+$/;

export function buildWordKey(segmentIndex, wordIndex) {
  return `${segmentIndex}:${wordIndex}`;
}

export function buildSilenceKey(start, end) {
  if (!Number.isFinite(start) || !Number.isFinite(end)) {
    return `silence:${Date.now()}`;
  }
  return `silence:${start.toFixed(3)}-${end.toFixed(3)}`;
}

export function buildSegmentTokens(segment, globalSegmentIndex, previousEnd) {
  const segmentIndex = globalIndexWithFallback(globalSegmentIndex);
  const segmentStart = Number(segment?.start);
  const segmentEnd = Number(segment?.end);
  const safeStart = Number.isFinite(segmentStart)
    ? segmentStart
    : (Number.isFinite(previousEnd) ? previousEnd : 0);
  const safeEnd = Number.isFinite(segmentEnd) ? segmentEnd : safeStart;

  const textTokens = parseSegmentText(segment?.text ?? "");
  if (!textTokens.length) {
    return { tokens: [], lastEnd: safeEnd };
  }

  const segments = prepareNormalizedWords(segment?.words ?? []);
  const sortedSegments = segments
    .filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end))
    .sort((a, b) => {
      const sa = Number.isFinite(a.start) ? a.start : Number.POSITIVE_INFINITY;
      const sb = Number.isFinite(b.start) ? b.start : Number.POSITIVE_INFINITY;
      if (sa === sb) {
        const ea = Number.isFinite(a.end) ? a.end : Number.POSITIVE_INFINITY;
        const eb = Number.isFinite(b.end) ? b.end : Number.POSITIVE_INFINITY;
        return ea - eb;
      }
      return sa - sb;
    });

  const assignments = allocateWordSegments(textTokens, sortedSegments);

  const resultTokens = [];
  const leadingGapTokens = [];
  if (Number.isFinite(previousEnd) && safeStart - previousEnd > GAP_EPSILON) {
    const prevSegmentIndex = segmentIndex > 0 ? segmentIndex - 1 : segmentIndex;
    pushGapTokens(leadingGapTokens, previousEnd, safeStart, {
      attachToPrevious: true,
      segmentIndex: prevSegmentIndex,
      targetSegmentIndex: prevSegmentIndex,
      allowShort: true,
    });
    resultTokens.push(...leadingGapTokens);
  }

  let wordSerial = 0;
  let cursor = safeStart;
  let pointer = 0;

  while (pointer < textTokens.length) {
    if (assignments[pointer]) {
      const assigned = assignments[pointer];
      const meta = textTokens[pointer];
      let start = Number.isFinite(assigned.start) ? assigned.start : cursor;
      let end = Number.isFinite(assigned.end) ? assigned.end : Math.max(start, cursor);
      if (start - cursor > GAP_EPSILON) {
        pushGapTokens(resultTokens, cursor, start, { segmentIndex });
        cursor = start;
      }
      if (start < cursor) {
        start = cursor;
      }
      if (end < start) {
        end = start;
      }
      const key = meta.type === "word"
        ? buildWordKey(segmentIndex, wordSerial)
        : buildSilenceKey(start, end);
      resultTokens.push({
        type: meta.type,
        text: meta.text,
        start,
        end,
        key,
        segmentIndex,
        wordIndex: meta.type === "word" ? wordSerial : null,
      });
      if (meta.type === "word") {
        wordSerial += 1;
      }
      cursor = end;
      pointer += 1;
      continue;
    }

    let endPointer = pointer;
    while (endPointer < textTokens.length && !assignments[endPointer]) {
      endPointer += 1;
    }
    const subsetTokens = textTokens.slice(pointer, endPointer);
    const subsetEnd = endPointer < textTokens.length && assignments[endPointer]
      ? Math.max(assignments[endPointer].start ?? cursor, cursor)
      : safeEnd;
    const distributed = distributeEvenlyTokens(
      subsetTokens,
      cursor,
      subsetEnd,
      segmentIndex,
      wordSerial,
    );
    distributed.tokens.forEach((tokenObj) => {
      resultTokens.push(tokenObj);
      cursor = tokenObj.end;
    });
    wordSerial = distributed.nextWordIndex ?? wordSerial;
    pointer = endPointer;
  }

  if (safeEnd - cursor > GAP_EPSILON) {
    pushGapTokens(resultTokens, cursor, safeEnd, { segmentIndex });
    cursor = safeEnd;
  }

  if (segmentIndex > 0) {
    for (let i = 0; i < resultTokens.length; i += 1) {
      const token = resultTokens[i];
      if (token.type !== "punctuation") {
        break;
      }
      if (token.attachToPrevious || token.segmentIndex != null && token.segmentIndex !== segmentIndex) {
        continue;
      }
      token.attachToPrevious = true;
      token.segmentIndex = segmentIndex - 1;
      token.targetSegmentIndex = segmentIndex;
    }
  }

  return { tokens: resultTokens, lastEnd: cursor, leadingGapTokens };
}

function parseSegmentText(rawText) {
  const tokens = [];
  if (!rawText) {
    return tokens;
  }

  let asciiBuffer = "";
  const flushAsciiBuffer = () => {
    if (!asciiBuffer) return;
    tokens.push({
      type: "word",
      text: asciiBuffer,
    });
    asciiBuffer = "";
  };

  for (const char of rawText) {
    if (/\s/u.test(char)) {
      flushAsciiBuffer();
      continue;
    }

    if (PUNCTUATION_CHAR_SET.has(char)) {
      flushAsciiBuffer();
      tokens.push({
        type: "punctuation",
        text: normalizeWordText(char),
      });
      continue;
    }

    const normalized = normalizeWordText(char);
    if (!normalized) {
      flushAsciiBuffer();
      continue;
    }

    if (ASCII_WORD_REGEX.test(normalized)) {
      asciiBuffer += normalized;
      continue;
    }

    flushAsciiBuffer();
    tokens.push({
      type: "word",
      text: normalized,
    });
  }

  flushAsciiBuffer();

  return tokens;
}

function prepareNormalizedWords(words) {
  if (!Array.isArray(words)) {
    return [];
  }
  const result = [];
  let lastEnd;
  words.forEach((word) => {
    const normalized = normalizeWordText(word?.text ?? "");
    let start = Number(word?.start);
    let end = Number(word?.end);
    if (!Number.isFinite(start) && Number.isFinite(lastEnd)) {
      start = lastEnd;
    }
    if (!Number.isFinite(end) && Number.isFinite(start)) {
      end = start + MIN_SEGMENT_DURATION;
    }
    if (Number.isFinite(end)) {
      lastEnd = end;
    }
    result.push({
      normalized,
      start,
      end,
    });
  });
  result.sort((a, b) => {
    const sa = Number.isFinite(a.start) ? a.start : Number.POSITIVE_INFINITY;
    const sb = Number.isFinite(b.start) ? b.start : Number.POSITIVE_INFINITY;
    if (sa === sb) {
      const ea = Number.isFinite(a.end) ? a.end : Number.POSITIVE_INFINITY;
      const eb = Number.isFinite(b.end) ? b.end : Number.POSITIVE_INFINITY;
      return ea - eb;
    }
    return sa - sb;
  });
  return result;
}

function allocateWordSegments(tokens, segments) {
  const assignments = new Array(tokens.length).fill(null);
  if (!tokens.length || !segments.length) {
    return assignments;
  }

  const units = [];
  tokens.forEach((token, index) => {
    if (!token || token.type !== "word") {
      return;
    }
    const ascii = isAsciiToken(token);
    units.push({
      index,
      ascii,
      weight: ascii ? Math.max(token.text.length, 1) : 1,
    });
  });

  if (!units.length) {
    return assignments;
  }

  const allocation = units.map(() => 1);
  let extra = segments.length - units.length;
  if (extra > 0) {
    const asciiUnits = units
      .map((unit, idx) => ({ idx, weight: unit.weight }))
      .filter((entry) => units[entry.idx].ascii);
    if (asciiUnits.length) {
      asciiUnits.sort((a, b) => {
        if (b.weight === a.weight) {
          return a.idx - b.idx;
        }
        return b.weight - a.weight;
      });
      let cursor = 0;
      while (extra > 0) {
        const pick = asciiUnits[cursor % asciiUnits.length];
        allocation[pick.idx] += 1;
        cursor += 1;
        extra -= 1;
      }
    } else {
      allocation[allocation.length - 1] += extra;
    }
  }

  let segmentCursor = 0;
  let lastAssignedIndex = -1;
  for (let i = 0; i < units.length; i += 1) {
    if (segmentCursor >= segments.length) {
      break;
    }
    const unit = units[i];
    const available = segments.length - segmentCursor;
    const desired = Math.max(1, allocation[i]);
    const consume = Math.min(desired, available);
    const startSegment = segments[segmentCursor];
    const endSegment = segments[segmentCursor + consume - 1];
    assignments[unit.index] = {
      start: startSegment?.start,
      end: endSegment?.end,
    };
    segmentCursor += consume;
    lastAssignedIndex = unit.index;
  }

  if (segmentCursor < segments.length && lastAssignedIndex >= 0) {
    const tail = segments[segments.length - 1];
    const assigned = assignments[lastAssignedIndex];
    if (assigned && Number.isFinite(assigned.start) && Number.isFinite(tail?.end)) {
      assigned.end = tail.end;
    }
  }

  return assignments;
}

function distributeEvenlyTokens(sourceTokens, start, end, segmentIndex, startingWordIndex = 0) {
  const safeStart = Number.isFinite(start) ? start : 0;
  let safeEnd = Number.isFinite(end) ? end : safeStart;
  if (safeEnd <= safeStart) {
    safeEnd = safeStart + Math.max(sourceTokens.length, 1) * (MIN_SEGMENT_DURATION);
  }
  if (!sourceTokens.length) {
    return { tokens: [], lastEnd: safeEnd };
  }

  const weights = sourceTokens.map((token) => Math.max([...token.text].length, 1));
  const totalWeight = weights.reduce((sum, value) => sum + value, 0) || sourceTokens.length;
  let cursor = safeStart;
  let wordSerial = startingWordIndex;
  const distributed = sourceTokens.map((token, index) => {
    const share = (safeEnd - safeStart) * (weights[index] / totalWeight);
    let next = index === sourceTokens.length - 1
      ? safeEnd
      : Math.min(safeEnd, cursor + share);
    if (next <= cursor) {
      next = Math.min(safeEnd, cursor + (safeEnd - safeStart) / totalWeight);
    }
    const key = token.type === "word"
      ? buildWordKey(segmentIndex, wordSerial)
      : buildSilenceKey(cursor, next);
    const output = {
      type: token.type,
      text: token.text,
      start: cursor,
      end: next,
      key,
      segmentIndex,
      wordIndex: token.type === "word" ? wordSerial : null,
    };
    if (token.type === "word") {
      wordSerial += 1;
    }
    cursor = next;
    return output;
  });

  if (distributed.length) {
    distributed[distributed.length - 1].end = safeEnd;
  }

  return { tokens: distributed, lastEnd: safeEnd, nextWordIndex: wordSerial };
}

function isAsciiToken(token) {
  return token?.type === "word" && ASCII_WORD_REGEX.test(token.text);
}

function normalizeWordText(text) {
  if (!text) return "";
  let result = String(text).replace(/\s+/gu, " ").trim();
  if (!result) return "";
  result = result.replace(/[A-Z]/g, (ch) => ch.toLowerCase());
  result = result
    .replace(/[\u3001\uFF64\uFF65\u203B]/gu, CH_COMMA)
    .replace(/[,\uFF0C]/gu, CH_COMMA)
    .replace(/[.\u3002]/gu, CH_PERIOD)
    .replace(/[;\uFF1B]/gu, CH_SEMICOLON)
    .replace(/[:\uFF1A]/gu, CH_COLON)
    .replace(/[!\uFF01]/gu, CH_EXCLAMATION)
    .replace(/[?\uFF1F]/gu, CH_QUESTION);
  return result;
}

function mapPunctuation(value) {
  if (!value) return null;
  if (value.includes(CH_EXCLAMATION)) return CH_EXCLAMATION;
  if (value.includes(CH_QUESTION)) return CH_QUESTION;
  if (value.includes(CH_SEMICOLON)) return CH_SEMICOLON;
  if (value.includes(CH_COLON)) return CH_COLON;
  if (value.includes(CH_PERIOD)) return CH_PERIOD;
  return CH_COMMA;
}

function globalIndexWithFallback(index) {
  return Number.isFinite(index) ? index : 0;
}

function pushGapTokens(target, gapStart, gapEnd, options = {}) {
  if (!Number.isFinite(gapStart) || !Number.isFinite(gapEnd)) return [];
  const duration = gapEnd - gapStart;
  if (duration <= GAP_EPSILON) {
    const last = target[target.length - 1];
    if (last && typeof last.end === "number" && last.end < gapEnd) {
      last.end = gapEnd;
    }
    return [];
  }

  const allowShort = Boolean(options.allowShort);
  const attachToPrevious = Boolean(options.attachToPrevious);
  const last = target[target.length - 1];

  if (!attachToPrevious && duration < INNER_PUNCTUATION_MIN_DURATION) {
    if (last && typeof last.end === "number" && last.end < gapEnd) {
      last.end = gapEnd;
    }
    return [];
  }

  const isShortGap = duration <= SILENCE_MIN_DURATION || (!allowShort && duration <= PUNCTUATION_MIN_DURATION);
  if (isShortGap && !attachToPrevious) {
    if (last && typeof last.end === "number" && last.end < gapEnd) {
      last.end = gapEnd;
    }
    return [];
  }

  const created = [];
  if (duration >= SILENCE_PLACEHOLDER_THRESHOLD && !attachToPrevious) {
    created.push({
      type: "silence",
      start: gapStart,
      end: gapEnd,
      key: buildSilenceKey(gapStart, gapEnd),
    });
  } else {
    const punctuation = isShortGap || duration < SILENCE_SENTENCE_THRESHOLD ? CH_COMMA : CH_PERIOD;
    const last = target[target.length - 1];
    if (
      last
      && last.type === "punctuation"
      && last.text === punctuation
      && Math.abs(last.end - gapStart) < 1e-3
    ) {
      last.end = gapEnd;
      return [];
    }
    created.push({
      type: "punctuation",
      text: punctuation,
      start: gapStart,
      end: gapEnd,
      key: buildSilenceKey(gapStart, gapEnd),
    });
  }

  created.forEach((token) => {
    token.attachToPrevious = Boolean(options.attachToPrevious);
    if (options.segmentIndex !== undefined) {
      token.segmentIndex = options.segmentIndex;
    }
    if (options.targetSegmentIndex !== undefined) {
      token.targetSegmentIndex = options.targetSegmentIndex;
    }
    target.push(token);
  });
  return created;
}

export function tokenizeTranscript(transcript) {
  const paginate = transcript?.pagination || {};
  const offset = paginate.offset || 0;
  const segments = Array.isArray(transcript?.segments) ? transcript.segments : [];

  const tokens = [];
  const boundaries = new Set();
  let previousEnd = null;

  segments.forEach((segment, segmentIdx) => {
    const globalSegmentIndex = offset + segmentIdx;
    const { tokens: segmentTokens, lastEnd } = buildSegmentTokens(segment, globalSegmentIndex, previousEnd);
    segmentTokens.forEach((token) => {
      if (!Number.isFinite(token.start) || !Number.isFinite(token.end) || token.end <= token.start) {
        return;
      }
      boundaries.add(Number(token.start.toFixed(6)));
      boundaries.add(Number(token.end.toFixed(6)));
      tokens.push({
        ...token,
        keys: [token.key],
      });
    });
    previousEnd = lastEnd;
  });

  const totalDuration = tokens.reduce((max, token) => Math.max(max, token.end), 0);
  const sortedBoundaries = Array.from(boundaries).sort((a, b) => a - b);

  return {
    tokens,
    boundaries: sortedBoundaries,
    duration: totalDuration,
    offset,
  };
}

export function mergeTranscriptTokens(transcripts) {
  if (!Array.isArray(transcripts)) {
    return tokenizeTranscript(transcripts);
  }
  const combined = {
    segments: [],
    pagination: { offset: 0 },
  };
  let offset = 0;
  transcripts.forEach((entry) => {
    if (!entry) return;
    const pagination = entry.pagination || {};
    const segments = Array.isArray(entry.segments) ? entry.segments : [];
    segments.forEach((segment) => {
      combined.segments.push(segment);
    });
    offset += pagination.returned || segments.length || 0;
  });
  return tokenizeTranscript(combined);
}
