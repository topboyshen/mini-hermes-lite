/**
 * Installed pi 0.85.1 CLI attachment pipeline, without starting a CLI session.
 *
 * Run explicitly on Linux inside a NEW network namespace, not via live pi:
 *   unshare --net -- env -i PATH=/usr/bin:/bin \
 *     /usr/bin/node --test /tmp/<candidate>/tests/test_pi_cli_attachments.mjs
 *
 * For a non-root test process, capture the host's /proc/self/ns/net readlink
 * before unshare and pass it as PI_NATIVE_HOST_NET_NAMESPACE. The test fails
 * closed if namespace isolation cannot be established. PI_NATIVE_PACKAGE_DIR
 * may select an isolated copy of the same installed package.
 *
 * Only pure CLI parsing/file ingestion/message conversion modules are loaded.
 * No Agent, auth store, login/refresh, real prompt, HTTP request or tool runs.
 * Synthetic fixtures are created in a private temporary directory and removed.
 */
import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { mkdtemp, readFile, readlink, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import tls from "node:tls";
import http from "node:http";
import https from "node:https";
import dgram from "node:dgram";
import childProcess from "node:child_process";

const packageDir = resolve(process.env.PI_NATIVE_PACKAGE_DIR
  || "/usr/lib/node_modules/@earendil-works/pi-coding-agent");
const aiDir = join(packageDir, "node_modules/@earendil-works/pi-ai/dist");
let fixtureDir;
let parseArgs, processFileArguments, buildInitialMessage, convertResponsesMessages;
let xaiOAuth, grok, photon, images;
let forbiddenCalls = 0;
const restorers = [];

function denyIO(target, key) {
  const original = target[key];
  target[key] = () => {
    forbiddenCalls += 1;
    throw new Error("native_attachment_test_forbids_network_or_process");
  };
  restorers.push(() => { target[key] = original; });
}

const importFile = (path) => import(pathToFileURL(path).href);

before(async () => {
  assert.equal(process.platform, "linux", "requires Linux network namespace isolation");
  const isolatedNamespace = await readlink("/proc/self/ns/net");
  const hostNamespace = process.env.PI_NATIVE_HOST_NET_NAMESPACE
    || await readlink("/proc/1/ns/net");
  assert.match(hostNamespace, /^net:\[\d+\]$/);
  assert.notEqual(isolatedNamespace, hostNamespace,
    "run under unshare --net; refusing an unisolated native test");

  // Defense in depth: even an accidental new dependency must not call out.
  denyIO(globalThis, "fetch");
  denyIO(net.Socket.prototype, "connect");
  for (const target of [http, https]) {
    denyIO(target, "request");
    denyIO(target, "get");
  }
  denyIO(tls, "connect");
  denyIO(dgram, "createSocket");
  for (const key of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"])
    denyIO(childProcess, key);
  syncBuiltinESMExports();

  const metadata = JSON.parse(await readFile(join(packageDir, "package.json"), "utf8"));
  assert.equal(metadata.version, "0.85.1", "re-review this native integration after pi upgrades");
  ({ parseArgs } = await importFile(join(packageDir, "dist/cli/args.js")));
  ({ processFileArguments } = await importFile(join(packageDir, "dist/cli/file-processor.js")));
  ({ buildInitialMessage } = await importFile(join(packageDir, "dist/cli/initial-message.js")));
  ({ convertResponsesMessages } = await importFile(join(aiDir, "api/openai-responses-shared.js")));
  ({ xaiOAuth } = await importFile(join(aiDir, "auth/oauth/xai.js")));
  const { XAI_MODELS } = await importFile(join(aiDir, "providers/xai.models.js"));
  grok = XAI_MODELS["grok-4.6"];
  const { loadPhoton } = await importFile(join(packageDir, "dist/utils/photon.js"));
  photon = await loadPhoton();
  assert.ok(photon?.PhotonImage, "native image decoder must be available, not skipped");
  fixtureDir = await mkdtemp(join(tmpdir(), "pi-native-attachments-"));
  // Genuine encoded images from synthetic pixels, not magic-header-only mocks.
  const pixel = new photon.PhotonImage(new Uint8Array([255, 0, 0, 255]), 1, 1);
  try {
    images = [
      { extension: "png", mime: "image/png", bytes: Buffer.from(pixel.get_bytes()) },
      { extension: "jpg", mime: "image/jpeg", bytes: Buffer.from(pixel.get_bytes_jpeg(80)) },
      { extension: "webp", mime: "image/webp", bytes: Buffer.from(pixel.get_bytes_webp()) },
    ];
  } finally {
    pixel.free();
  }
});

after(async () => {
  try {
    if (fixtureDir) await rm(fixtureDir, { recursive: true, force: true });
    assert.equal(forbiddenCalls, 0, "native pure-function pipeline attempted forbidden I/O");
  } finally {
    for (const restore of restorers.reverse()) restore();
    syncBuiltinESMExports();
  }
});

function parseBrokerArguments(file, prompt) {
  return parseArgs([
    "--offline", "--mode", "json", "--model", "grok-4.6", "--thinking", "low",
    "--append-system-prompt", "Synthetic attachment boundary", "-p", "--",
    "@" + file,
    prompt.startsWith("@") ? "User message:\n" + prompt : prompt,
  ]);
}

async function initialInput(file, prompt = "请概括这份测试附件") {
  const parsed = parseBrokerArguments(file, prompt);
  assert.deepEqual(parsed.fileArgs, [file]);
  assert.equal(parsed.messages.length, 1);
  assert.equal(parsed.mode, "json");
  assert.equal(parsed.model, "grok-4.6");
  assert.equal(parsed.thinking, "low");
  // Deliberately omit options: validate pi's real default autoResizeImages=true.
  const { text, images: fileImages } = await processFileArguments(parsed.fileArgs);
  const initial = buildInitialMessage({ parsed, fileText: text, fileImages });
  assert.equal(parsed.messages.length, 0, "initial prompt consumed exactly once");
  assert.ok(initial.initialMessage.includes(prompt));
  return initial;
}

test("installed Grok supports image and synthetic OAuth preserves its API route", async () => {
  assert.equal(grok.id, "grok-4.6");
  assert.equal(grok.provider, "xai");
  assert.equal(grok.api, "openai-responses");
  assert.equal(grok.baseUrl, "https://api.x.ai/v1");
  assert.deepEqual(grok.input, ["text", "image"]);
  // toAuth is pure. Never call login or refresh, and never open an auth store.
  const auth = await xaiOAuth.toAuth({ type: "oauth", access: "synthetic" });
  assert.deepEqual(auth, { apiKey: "synthetic" });
  assert.equal(auth.model, undefined);
  assert.equal(auth.baseUrl, undefined);
});

test("installed pi parses all four menu efforts supported by Grok 4.6", () => {
  for (const level of ["low", "medium", "high", "xhigh"]) {
    const parsed = parseArgs(["--offline", "--mode", "json", "--model", "xai/grok-4.6",
      "--thinking", level, "-p", "--", "synthetic"]);
    assert.equal(parsed.thinking, level);
    assert.equal(grok.thinkingLevelMap[level], level);
    assert.equal(grok.reasoning, true);
  }
});

for (const extension of ["png", "jpg", "webp"]) {
  test(`native CLI ingests and Responses preserves ${extension} image`, async () => {
    const fixture = images.find((entry) => entry.extension === extension);
    const file = join(fixtureDir, "synthetic." + extension);
    await writeFile(file, fixture.bytes, { mode: 0o600 });
    const initial = await initialInput(file);
    assert.equal(initial.initialImages?.length, 1, "image must not silently become omitted text");
    const image = initial.initialImages[0];
    assert.equal(image.type, "image");
    assert.equal(image.mimeType, fixture.mime);
    assert.deepEqual(Buffer.from(image.data, "base64"), fixture.bytes);
    assert.equal(initial.initialMessage.includes("Image omitted"), false);

    // Decode what pi will send, using its own installed WASM image decoder.
    const decoded = photon.PhotonImage.new_from_byteslice(Buffer.from(image.data, "base64"));
    try {
      assert.equal(decoded.get_width(), 1);
      assert.equal(decoded.get_height(), 1);
    } finally {
      decoded.free();
    }
    const converted = convertResponsesMessages(grok, {
      messages: [{ role: "user", content: [
        { type: "text", text: initial.initialMessage }, image,
      ], timestamp: 0 }],
    }, new Set(["xai"]));
    const user = converted.find((item) => item.role === "user");
    assert.deepEqual(user.content, [
      { type: "input_text", text: initial.initialMessage },
      { type: "input_image", detail: "auto", image_url: `data:${fixture.mime};base64,${image.data}` },
    ]);
  });
}

test("native CLI preserves UTF-8 TXT MD CSV JSON as inline text, without images", async () => {
  for (const extension of ["txt", "md", "csv", "json"]) {
    const file = join(fixtureDir, "synthetic." + extension);
    const text = "测试内容：不运行命令、不读取外部文件\n";
    await writeFile(file, "\ufeff" + text, { mode: 0o600 });
    const initial = await initialInput(file);
    assert.equal(initial.initialImages, undefined);
    assert.ok(initial.initialMessage.includes(`<file name="${file}">\n${text}\n</file>`));
    assert.equal(initial.initialMessage.includes("\ufeff"), false);
  }
});

test("actual CLI parsing cannot reinterpret guarded prompt as another file or flag", async () => {
  const file = join(fixtureDir, "synthetic-prompt.txt");
  await writeFile(file, "Synthetic safe input", { mode: 0o600 });
  for (const prompt of ["@/synthetic/not-read", "--provider synthetic", "--api-key synthetic"]) {
    const parsed = parseBrokerArguments(file, prompt);
    assert.deepEqual(parsed.fileArgs, [file]);
    assert.equal(parsed.provider, undefined);
    assert.equal(parsed.apiKey, undefined);
    assert.equal(parsed.messages.length, 1);
    await initialInput(file, prompt);
  }
});

test("Responses conversion does not silently accept images for a text-only model", () => {
  const converted = convertResponsesMessages({ ...grok, input: ["text"] }, {
    messages: [{ role: "user", content: [{ type: "image", mimeType: "image/png", data: "synthetic" }], timestamp: 0 }],
  }, new Set(["xai"]));
  assert.equal(converted[0].content.some((item) => item.type === "input_image"), false);
  assert.ok(converted[0].content.some((item) => item.type === "input_text" && /omitted/.test(item.text)));
});
