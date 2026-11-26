// Meme-Bot Dauertest: startet die Bot-Seite in einem unsichtbaren Chrome auf GitHub Actions,
// lässt sie mehrere Stunden im TESTMODUS (Testgeld) laufen und speichert den Stand regelmäßig
// im Branch "memebot-data". Echter Handel ist in der Seite fest gesperrt.
const puppeteer = require("puppeteer-core");
const fs = require("fs");
const path = require("path");
const { execSync } = require("child_process");

const URL = "https://geiserjulian377-glitch.github.io/tr-bot-bf82a4ecc457/memebot/?server=1";
const RUN_MIN = +(process.env.RUN_MIN || 330);
const DATA = path.resolve("data");
const ST = path.join(DATA, "state");
const read = f => { try { return fs.readFileSync(path.join(ST, f), "utf8"); } catch (e) { return null; } };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const t0 = Date.now();
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a);

function git(cmd) { return execSync(`git -C "${DATA}" ${cmd}`, { stdio: "pipe" }).toString(); }
function commit(msg) {
  try {
    git("add -A");
    try { git(`commit -q -m "${msg}"`); } catch (e) { return; }   // nichts zu committen
    for (let i = 0; i < 4; i++) {
      try { git("push -q origin HEAD:memebot-data"); log("gespeichert:", msg); return; }
      catch (e) { try { git("pull -q --rebase origin memebot-data"); } catch (e2) {} }
    }
  } catch (e) { log("Commit-Fehler", e.message); }
}

async function main() {
  if (fs.existsSync(path.join(ST, "STOP"))) { log("STOP-Datei gefunden – Dauertest ist angehalten."); fs.writeFileSync("STOPPED", "1"); return; }
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME || "/usr/bin/google-chrome", headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--disable-background-timer-throttling", "--disable-renderer-backgrounding"]
  });
  const page = await browser.newPage();
  const seedState = read("state.json"), seedCfg = read("cfg.json"), seedVer = read("cfgVer.txt"), brainSeed = read("seed.json");
  await page.evaluateOnNewDocument((st, cfg, ver) => {
    if (sessionStorage.getItem("seeded")) return;
    if (st) localStorage.setItem("mb_state", st);
    if (cfg) localStorage.setItem("mb_cfg", cfg);
    localStorage.setItem("mb_cfgVer", ver || "5");
    sessionStorage.setItem("seeded", "1");
  }, seedState, seedCfg, seedVer && seedVer.trim());
  // API-Schlüssel aus GitHub-Secrets nur in den Browser geben – nie speichern oder committen
  const keys = {}; if (process.env.HELIUS_KEY) keys.helius = process.env.HELIUS_KEY; if (process.env.JUP_KEY) keys.jup = process.env.JUP_KEY;
  await page.evaluateOnNewDocument(k => { try { localStorage.setItem("mb_keys", k); } catch (e) {} }, JSON.stringify(keys));
  log("Schlüssel:", Object.keys(keys).join(", ") || "keine");
  page.on("pageerror", e => log("Seitenfehler:", e.message));

  async function open() {
    await page.goto(URL, { waitUntil: "networkidle2", timeout: 90000 }).catch(e => log("Ladefehler", e.message));
    await page.waitForSelector("#start", { timeout: 60000 });
    if (!seedState && brainSeed) {   // erster Lauf: frisches Testkonto, aber mit dem gelernten Gehirn
      await page.evaluate(seed => { const S = window.__memebot.S; if (!S.trades.length) Object.assign(S, JSON.parse(seed)); }, brainSeed);
    }
    await page.evaluate(() => { const b = document.querySelector("#start"); if (b && !b.hidden) b.click(); });
    log("Bot gestartet:", await page.evaluate(() => document.body.innerText.match(/v\d+ · test/)?.[0]));
  }

  async function save() {
    const d = await page.evaluate(() => {
      const S = window.__memebot && window.__memebot.S; if (!S) return null;
      const eq = S.bal + S.pos.reduce((a, p) => a + p.qty * (p.last || p.entry), 0);
      const B = S.brain || {};
      return {
        state: localStorage.getItem("mb_state"), cfg: localStorage.getItem("mb_cfg"), ver: localStorage.getItem("mb_cfgVer"),
        summary: {
          updated: Date.now(), version: document.body.innerText.match(/v\d+ · test/)?.[0] || "", startBal: S.startBal, since: S.start,
          equity: Math.round(eq * 100) / 100, cash: Math.round(S.bal * 100) / 100, trades: S.trades.length, wins: S.wins, losses: S.losses,
          pnl: Math.round(S.trades.reduce((a, t) => a + (t.got - t.cost), 0) * 100) / 100,
          open: S.pos.map(p => ({ sym: p.sym, mode: p.mode, cost: p.cost, gain: (p.last / p.entry - 1) * 100, min: Math.round((Date.now() - p.t0) / 60000) })),
          last: S.trades.slice(-15).reverse().map(t => ({ sym: t.sym, mode: t.mode, ret: t.ret, cost: t.cost, why: t.why, t1: t.t1 })),
          brain: { n: Math.round(B.n || 0), acc: B.seen ? Math.round(B.hits / B.seen * 100) : 0 }, lage: S.lage ? S.lage.name : "",
          tune: S.tune || null, minProb: window.__memebot.C.minProb,
          days: Object.entries(S.trades.reduce((m, t) => { const k = new Date(t.t1).toISOString().slice(0, 10); m[k] = m[k] || {n: 0, w: 0, pnl: 0}; m[k].n++; m[k].w += t.ret > 0 ? 1 : 0; m[k].pnl += t.got - t.cost; return m; }, {})).slice(-7),
          shadows: (S.shadow || []).filter(x => x.done).length, scanned: S.stats.scan,
          log: S.log.slice(-25).reverse().map(l => [l.t, l.type, l.sym, l.text.slice(0, 220), l.val])
        }
      };
    }).catch(e => { log("Lesefehler", e.message); return null; });
    if (!d || !d.state) return null;
    fs.mkdirSync(ST, { recursive: true });
    fs.writeFileSync(path.join(ST, "state.json"), d.state);
    if (d.cfg) fs.writeFileSync(path.join(ST, "cfg.json"), d.cfg);
    if (d.ver) fs.writeFileSync(path.join(ST, "cfgVer.txt"), d.ver);
    fs.writeFileSync(path.join(ST, "summary.json"), JSON.stringify(d.summary, null, 1));
    return d.summary;
  }

  await open();
  let lastSave = 0, lastCommit = Date.now(), lastReload = Date.now(), firstCommitted = false;
  while (Date.now() - t0 < RUN_MIN * 60000) {
    await sleep(60000);
    if (Date.now() - lastSave > 5 * 60000) {
      const s = await save(); lastSave = Date.now();
      if (s) log(`Konto $${s.equity} · ${s.trades} Trades (${s.wins}/${s.losses}) · offen ${s.open.length} · Gehirn ${s.brain.n}/${s.brain.acc}%`);
      else { log("Seite reagiert nicht – lade neu"); await open().catch(e => log(e.message)); }
    }
    if (Date.now() - lastCommit > 20 * 60000 || !firstCommitted) { if (fs.existsSync(path.join(ST, "summary.json"))) { commit("Dauertest-Zwischenstand"); firstCommitted = true; } lastCommit = Date.now(); }
    if (Date.now() - lastReload > 120 * 60000) {   // alle 2 Std. neu laden: neueste Bot-Version übernehmen
      await save(); await page.evaluate(() => document.querySelector("#stop")?.click()).catch(() => {});
      await sleep(3000); await open().catch(e => log(e.message)); lastReload = Date.now();
    }
  }
  await page.evaluate(() => document.querySelector("#stop")?.click()).catch(() => {});
  await sleep(3000);
  await save();
  commit("Dauertest-Lauf beendet");
  await browser.close();
}

main().catch(e => { console.error(e); process.exitCode = 1; });
