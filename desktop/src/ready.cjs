"use strict";

function validReady(line) {
  if (!line.startsWith("READY ")) return null;
  try {
    const record = JSON.parse(line.slice(6));
    return Number.isInteger(record.port) && record.port > 0 && record.port <= 65535 ? record : null;
  } catch (_) { return null; }
}

module.exports = { validReady };
