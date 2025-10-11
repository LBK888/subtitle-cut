export const SILENCE_COMMA_THRESHOLD = 0.45;
export const SILENCE_PLACEHOLDER_THRESHOLD = 1.2;

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
  const tokens = [];
  if (!segment) {
    return { tokens, lastEnd: previousEnd };
  }

  const words = Array.isArray(segment.words) ? segment.words : [];
  let lastEnd = Number.isFinite(Number(segment.start)) ? Number(segment.start) : previousEnd;

  const firstWordStart = Number(words[0]?.start ?? segment.start ?? previousEnd);
  if (Number.isFinite(previousEnd) && Number.isFinite(firstWordStart)) {
    pushGapTokens(tokens, previousEnd, firstWordStart);
  }

  words.forEach((word, wordIdx) => {
    const start = Number(word?.start ?? lastEnd ?? 0);
    if (Number.isFinite(lastEnd)) {
      pushGapTokens(tokens, lastEnd, start);
    }

    const end = Number(word?.end ?? start);
    const safeStart = Number.isFinite(start) ? start : (Number.isFinite(lastEnd) ? lastEnd : 0);
    const safeEnd = Number.isFinite(end) ? end : safeStart;
    const textValue = String(word?.text ?? "");

    tokens.push({
      type: "word",
      text: textValue,
      start: safeStart,
      end: safeEnd,
      key: buildWordKey(globalSegmentIndex, wordIdx),
      segmentIndex: globalSegmentIndex,
      wordIndex: wordIdx,
    });

    lastEnd = safeEnd;
  });

  const segmentEnd = Number(segment?.end);
  if (Number.isFinite(segmentEnd) && (!Number.isFinite(lastEnd) || segmentEnd > lastEnd)) {
    lastEnd = segmentEnd;
  }

  return { tokens, lastEnd };
}

function pushGapTokens(tokens, gapStart, gapEnd) {
  if (!Number.isFinite(gapStart) || !Number.isFinite(gapEnd)) return;
  const duration = gapEnd - gapStart;
  if (duration <= 0 || duration < SILENCE_COMMA_THRESHOLD) {
    return;
  }
  if (duration >= SILENCE_PLACEHOLDER_THRESHOLD) {
    tokens.push({
      type: "silence",
      start: gapStart,
      end: gapEnd,
      key: buildSilenceKey(gapStart, gapEnd),
    });
  } else {
    const punctuation = duration >= 0.8 ? "。" : "，";
    tokens.push({
      type: "punctuation",
      text: punctuation,
      start: gapStart,
      end: gapEnd,
      key: buildSilenceKey(gapStart, gapEnd),
    });
  }
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
    segments.forEach((segment, idx) => {
      combined.segments.push(segment);
    });
    offset += pagination.returned || segments.length || 0;
  });
  return tokenizeTranscript(combined);
}
