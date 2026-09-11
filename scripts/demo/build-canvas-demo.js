// Records the "building a project on the canvas" demo used in the top-level
// README (docs/images/canvas-demo.gif).
//
// Storyboard:
//   blank project -> Showroom -> Gateway (auto-connects to the showroom)
//   -> Network -> VM -> OpenShift Cluster -> wire them up -> Auto Layout.
//
// Why a standalone Playwright script (not the Playwright MCP): recording a webm
// requires `recordVideo` on the browser context, which only a script controls.
//
// Palette items drop onto the canvas via HTML5 drag-and-drop, which Chromium
// does NOT drive from synthetic mouse moves — so we dispatch drag events with a
// shared DataTransfer handle (see dragItem). Node-to-node edges use React Flow
// handles (pointer events), which a real mouse drag DOES drive (see connect).
// The OS cursor isn't captured in the video, so we inject a fake SVG cursor and
// animate it alongside the real actions.
//
// Usage:
//   cd scripts/demo && npm install playwright && npx playwright install chromium
//   node build-canvas-demo.js                 # -> scripts/demo/video/*.webm
//   TROSHKA_URL=http://localhost:3100 node build-canvas-demo.js
//
// Then convert to the GIF committed in the repo (ffmpeg required):
//   IN=video/*.webm
//   ffmpeg -y -i $IN -vf "setpts=0.72*PTS,fps=10,scale=900:-1:flags=lanczos,palettegen=max_colors=160:stats_mode=diff" palette.png
//   ffmpeg -y -i $IN -i palette.png -lavfi "setpts=0.72*PTS,fps=10,scale=900:-1:flags=lanczos,paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" ../../docs/images/canvas-demo.gif
//
// The dev frontend must be running (./dev-services.sh start) and in dev mode it
// auto-authenticates as admin. A throwaway "Demo Lab" project is created and
// deleted again at the end.
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.env.TROSHKA_URL || "http://localhost:3100";
const OUT_DIR = process.env.DEMO_OUT || path.join(__dirname, "video");
const VW = 1440, VH = 900;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function injectCursor(page) {
  await page.evaluate(() => {
    if (document.getElementById("__demo_cursor")) return;
    const c = document.createElement("div");
    c.id = "__demo_cursor";
    Object.assign(c.style, {
      position: "fixed", left: "0px", top: "0px", width: "24px", height: "24px",
      zIndex: "2147483647", pointerEvents: "none",
      filter: "drop-shadow(0 1px 2px rgba(0,0,0,0.6))",
    });
    const svg =
      '<svg width="24" height="24" viewBox="0 0 24 24">' +
      '<path d="M3 2 L3 19 L8.2 14.2 L11.3 21 L14 19.9 L11 13.4 L18 13 Z" ' +
      'fill="#ffffff" stroke="#111" stroke-width="1.2" stroke-linejoin="round"/></svg>';
    c.appendChild(new DOMParser().parseFromString(svg, "image/svg+xml").documentElement);
    document.body.appendChild(c);
    window.__cur = { x: 260, y: 260 };
    window.__moveCursor = (x, y) => { c.style.left = x + "px"; c.style.top = y + "px"; window.__cur = { x, y }; };
    window.__moveCursor(260, 260);
  });
}

async function glide(page, x, y, steps = 26) {
  await page.evaluate(async ({ x, y, steps }) => {
    const s = window.__cur || { x, y };
    const sx = s.x, sy = s.y;
    const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);
    for (let i = 1; i <= steps; i++) {
      const e = ease(i / steps);
      window.__moveCursor(sx + (x - sx) * e, sy + (y - sy) * e);
      await new Promise((r) => setTimeout(r, 16));
    }
  }, { x, y, steps });
}

async function expandSection(page, title) {
  const sec = page.locator(`.palette-section-title:has-text("${title}")`).first();
  const box = await sec.boundingBox();
  if (box) { await glide(page, box.x + 30, box.y + box.height / 2, 16); await sleep(150); }
  await sec.click();
  await sleep(450);
}

// Palette -> canvas via HTML5 DnD with a shared DataTransfer.
async function dragItem(page, label, tx, ty) {
  const item = page.locator(`.palette-item:has(.palette-item-label:text-is("${label}"))`).first();
  await item.scrollIntoViewIfNeeded();
  const box = await item.boundingBox();
  await glide(page, box.x + box.width / 2, box.y + box.height / 2, 22);
  await sleep(450);

  const src = await item.elementHandle();
  const pane = (await page.$(".react-flow__pane")) || (await page.$(".react-flow"));
  const dt = await page.evaluateHandle(() => new DataTransfer());
  await src.dispatchEvent("dragstart", { dataTransfer: dt });
  await glide(page, tx, ty, 30); // visual drag motion
  await pane.dispatchEvent("dragenter", { dataTransfer: dt, clientX: tx, clientY: ty });
  await pane.dispatchEvent("dragover", { dataTransfer: dt, clientX: tx, clientY: ty });
  await pane.dispatchEvent("drop", { dataTransfer: dt, clientX: tx, clientY: ty });
  await src.dispatchEvent("dragend", { dataTransfer: dt });
  await sleep(850);
}

async function handleCenter(page, nodeSel, pos) {
  const h = page.locator(`${nodeSel} .react-flow__handle-${pos}`).first();
  try { await h.waitFor({ state: "attached", timeout: 3000 }); } catch { return null; }
  const b = await h.boundingBox();
  return b ? { x: b.x + b.width / 2, y: b.y + b.height / 2 } : null;
}

const edgeCount = (page) =>
  page.evaluate(() => document.querySelectorAll(".react-flow__edge").length);

// Draw a React Flow edge by dragging between two handles (pointer events).
// Returns true only if an edge actually formed (drop on the far handle can be
// rejected or snap to a neighbour, so callers verify + fall back).
async function connect(page, srcSel, srcPos, dstSel, dstPos) {
  const s = await handleCenter(page, srcSel, srcPos);
  const d = await handleCenter(page, dstSel, dstPos);
  if (!s || !d) { console.log("connect: handle missing", srcSel, srcPos, "->", dstSel, dstPos); return false; }
  const before = await edgeCount(page);
  await glide(page, s.x, s.y, 18);
  await sleep(200);
  await page.mouse.move(s.x, s.y);
  await page.mouse.down();
  const steps = 26;
  for (let i = 1; i <= steps; i++) {
    const t = i / steps, x = s.x + (d.x - s.x) * t, y = s.y + (d.y - s.y) * t;
    await page.mouse.move(x, y);
    await page.evaluate(({ x, y }) => window.__moveCursor && window.__moveCursor(x, y), { x, y });
    await sleep(12);
  }
  await page.mouse.move(d.x, d.y);
  await page.mouse.up();
  await sleep(550);
  const ok = (await edgeCount(page)) > before;
  if (!ok) console.log("connect: no edge formed", srcSel, srcPos, "->", dstSel, dstPos);
  return ok;
}

// Try handle combinations until one produces an edge.
async function connectAny(page, combos) {
  for (const [ss, sp, ds, dp] of combos) {
    if (await connect(page, ss, sp, ds, dp)) return true;
  }
  return false;
}

// Drag a node left by `dx` px, grabbing its header (the cluster box body is
// pointerEvents:none; only the header strip is draggable).
async function dragNodeLeft(page, nodeSel, dx) {
  const n = page.locator(nodeSel).first();
  const box = await n.boundingBox();
  if (!box) { console.log("dragNodeLeft: node missing", nodeSel); return; }
  const gx = box.x + 70, gy = box.y + 12; // header strip, clear of the Status button
  await glide(page, gx, gy, 18);
  await sleep(200);
  await page.mouse.move(gx, gy);
  await page.mouse.down();
  const steps = 22;
  for (let i = 1; i <= steps; i++) {
    const x = gx - dx * (i / steps);
    await page.mouse.move(x, gy);
    await page.evaluate(({ x, y }) => window.__moveCursor && window.__moveCursor(x, y), { x, y: gy });
    await sleep(14);
  }
  await page.mouse.up();
  await sleep(600);
}

// Click the toolbar Auto Layout button (it re-fits the view afterwards).
async function autoLayout(page, hold = 1300) {
  const auto = page.locator('button[title="Auto Layout"]').first();
  if (!(await auto.count())) return;
  const ab = await auto.boundingBox();
  await glide(page, ab.x + ab.width / 2, ab.y + ab.height / 2, 14);
  await sleep(150); await auto.click();
  await sleep(hold);
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: VW, height: VH },
    deviceScaleFactor: 1,
    recordVideo: { dir: OUT_DIR, size: { width: VW, height: VH } },
  });
  const page = await context.newPage();
  let projectId = null;

  try {
    // 1) Projects page -> New Project -> Blank -> name -> create
    await page.goto(`${BASE}/projects`, { waitUntil: "networkidle" });
    await page.waitForSelector('button:has-text("New Project")', { timeout: 15000 });
    await injectCursor(page);
    await sleep(900);

    const nb = page.locator('button:has-text("New Project")').first();
    let b = await nb.boundingBox();
    await glide(page, b.x + b.width / 2, b.y + b.height / 2, 20);
    await sleep(300); await nb.click(); await sleep(700);

    const blank = page.locator("text=Blank Project").first();
    b = await blank.boundingBox();
    await glide(page, b.x + b.width / 2, b.y + b.height / 2, 18);
    await sleep(300); await blank.click(); await sleep(600);

    const nameInput = page.locator('input[placeholder="My Project"]').first();
    b = await nameInput.boundingBox();
    await glide(page, b.x + 40, b.y + b.height / 2, 14);
    await sleep(250); await nameInput.click();
    await nameInput.type("Demo Lab", { delay: 90 });
    await sleep(500);

    const createBtn = page.locator('button:has-text("Create Project")').first();
    b = await createBtn.boundingBox();
    await glide(page, b.x + b.width / 2, b.y + b.height / 2, 18);
    await sleep(300);
    await Promise.all([
      page.waitForURL(/\/projects\/[0-9a-f-]{36}/, { timeout: 20000 }),
      createBtn.click(),
    ]);
    projectId = page.url().split("/projects/")[1].split(/[/?#]/)[0];

    // 2) Canvas: wait for palette + pane
    await page.waitForSelector(".canvas-palette", { timeout: 15000 });
    await page.waitForSelector(".react-flow__pane", { timeout: 15000 });
    await sleep(1500);
    await injectCursor(page);
    await sleep(600);

    // Collapse the properties panel -> full-width canvas for the whole build.
    const hideProps = page.locator('button[title="Hide properties"]').first();
    if (await hideProps.count()) { await hideProps.click(); await sleep(500); }

    // Selectors. networkNode covers both networks and gateways, so scope by
    // visible label text.
    const gwSel = '.react-flow__node-networkNode:has-text("gateway")';
    const netSel = '.react-flow__node-networkNode:has-text("network-00")';
    const vmSel = '.react-flow__node-vmNode:has-text("vm-00")';
    const clusterSel = ".react-flow__node-clusterNode";

    // 3) Build incrementally: drop each object, connect it to the previous one,
    // then Auto Layout — so the topology stays tidy the whole way through and a
    // new object is only added after the last one is wired in.

    // Showroom (the lab guide UI)
    await expandSection(page, "Containers");
    await dragItem(page, "Showroom", 700, 340);
    await sleep(500);

    // Gateway -> auto-connects to the showroom on drop
    await expandSection(page, "Networking");
    await dragItem(page, "Gateway", 1050, 340);
    await sleep(400);
    await autoLayout(page);

    // Network -> connect to the gateway
    await dragItem(page, "Network", 1050, 600);
    await connectAny(page, [[gwSel, "bottom", netSel, "top"], [netSel, "top", gwSel, "bottom"]]);
    await autoLayout(page);

    // VM -> connect to the network (drop onto the VM, which sits in open space,
    // so the endpoint doesn't snap to a neighbouring node)
    await expandSection(page, "Compute");
    await dragItem(page, "VM", 1250, 600);
    await connectAny(page, [
      [netSel, "bottom", vmSel, "top"],
      [vmSel, "top", netSel, "bottom"],
      [vmSel, "bottom", netSel, "top"],
    ]);
    await autoLayout(page);

    // OpenShift Cluster -> connect to the network
    await dragItem(page, "OpenShift Cluster", 900, 780);
    await connectAny(page, [[netSel, "bottom", clusterSel, "top"], [clusterSel, "top", netSel, "bottom"]]);
    await autoLayout(page, 1600);

    // The cluster box carries a wide "drop VMs here" affordance in draft mode, so
    // Auto Layout leaves it crowding the standalone VM. Nudge it left for a clean gap.
    await dragNodeLeft(page, clusterSel, 200);

    // 4) Deselect (drops the selection outline) and frame the finished topology.
    await page.mouse.click(600, 250); // empty canvas area
    await sleep(400);
    const fit = page.locator('button[title="Fit View"]').first();
    if (await fit.count()) {
      const fb = await fit.boundingBox();
      await glide(page, fb.x + fb.width / 2, fb.y + fb.height / 2, 14);
      await sleep(200); await fit.click();
    }
    await sleep(2600);

    console.log("BUILT project", projectId, "nodes=",
      await page.evaluate(() => document.querySelectorAll(".react-flow__node").length));
  } catch (e) {
    console.error("DEMO_ERROR", (e && e.stack) || e);
  } finally {
    await context.close(); // finalizes the webm
    await browser.close();
    // Delete the throwaway demo project (best effort; Node 18+ has global fetch).
    if (projectId) {
      try { await fetch(`${BASE}/api/v1/projects/${projectId}`, { method: "DELETE" }); } catch {}
    }
    console.log("DONE projectId=", projectId);
  }
})();
