'use strict';
// Follows the agent's log file: reads what's there, then polls for new lines. Polling (not
// fs.watch) because it behaves the same on every system, survives the file being rotated to
// nexguard.log.1 or not existing yet, and costs one stat a second.
const fs = require('node:fs/promises');
const { parseLine } = require('./parse');

const MAX_INITIAL = 5000; // lines shown on open; the file is at most ~2 MB anyway

async function readEntries(file) {
  try {
    const text = await fs.readFile(file, 'utf8');
    return text.split('\n').map(parseLine).filter(Boolean);
  } catch (err) {
    if (err.code === 'ENOENT') return [];
    throw err;
  }
}

/** Everything in the rotated file and the current one, oldest first. */
async function readAll(log) {
  const entries = [...(await readEntries(log + '.1')), ...(await readEntries(log))];
  return entries.slice(-MAX_INITIAL);
}

class Tailer {
  /**
   * @param {string} file
   * @param {(entries: object[], info: {rotated: boolean}) => void} onEntries
   * @param {{interval?: number}} [opts]
   */
  constructor(file, onEntries, { interval = 1000 } = {}) {
    this.file = file;
    this.onEntries = onEntries;
    this.interval = interval;
    this.offset = 0;
    this.ino = null;
    this.timer = null;
    this.busy = false;
  }

  /** Start after what's already in the file (the caller shows that with readAll). */
  async start() {
    try {
      const st = await fs.stat(this.file);
      this.offset = st.size;
      this.ino = st.ino;
    } catch {
      this.offset = 0;
    }
    this.timer = setInterval(() => this.poll(), this.interval);
  }

  stop() {
    clearInterval(this.timer);
    this.timer = null;
  }

  async poll() {
    if (this.busy) return;
    this.busy = true;
    try {
      let st;
      try {
        st = await fs.stat(this.file);
      } catch {
        return; // not there (yet, or mid-rotation)
      }
      let rotated = false;
      const before = [];
      if (st.size < this.offset || (this.ino !== null && st.ino !== this.ino && st.ino !== 0)) {
        rotated = true;
        // Lines written just before the rename are now at the end of nexguard.log.1.
        const tail = await this.readRange(this.file + '.1', this.ino, this.offset);
        before.push(...tail);
        this.offset = 0;
      }
      this.ino = st.ino;
      let lines = [];
      if (st.size > this.offset) {
        // Advance only past whole lines, so a character split across two reads stays whole.
        const buf = await readFrom(this.file, this.offset, st.size);
        const end = buf.lastIndexOf(0x0a) + 1;
        this.offset += end;
        lines = buf.subarray(0, end).toString('utf8').split('\n').slice(0, -1);
      }
      const entries = [...before, ...lines.map(parseLine).filter(Boolean)];
      if (entries.length || rotated) this.onEntries(entries, { rotated });
    } catch {
      // unreadable for a moment (Windows sharing, permissions changed): try next tick
    } finally {
      this.busy = false;
    }
  }

  /** Whole lines after offset in file, if it is still the file we were reading. */
  async readRange(file, ino, offset) {
    try {
      const st = await fs.stat(file);
      if (ino === null || st.ino !== ino || st.size <= offset) return [];
      const text = (await readFrom(file, offset, st.size)).toString('utf8');
      return text.split('\n').map(parseLine).filter(Boolean);
    } catch {
      return [];
    }
  }
}

async function readFrom(file, start, end) {
  const fh = await fs.open(file, 'r');
  try {
    const buf = Buffer.alloc(end - start);
    const { bytesRead } = await fh.read(buf, 0, buf.length, start);
    return buf.subarray(0, bytesRead);
  } finally {
    await fh.close();
  }
}

module.exports = { Tailer, readAll, MAX_INITIAL };
