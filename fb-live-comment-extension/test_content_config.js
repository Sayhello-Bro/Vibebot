const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "chrome_extension", "content.js"), "utf8");
const configFunction = source.slice(
  source.indexOf("function readAutoConfig()"),
  source.indexOf("function cleanProfileName(")
);

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key)
  };
}

function config(url, sessionStorage) {
  return vm.runInNewContext(
    `const SESSION_CONFIG_KEY = "fb_auto_session"; ${configFunction} readAutoConfig()`,
    { window: { location: new URL(url) }, sessionStorage, URLSearchParams }
  );
}

const tabOne = storage();
const tabTwo = storage();
const firstFile = "C:\\sessions\\fb_account_003_live_01.jsonl";
const secondFile = "C:\\sessions\\fb_account_003_live_02.jsonl";
const facebook = "https://www.facebook.com/watch/";

const one = config(`${facebook}?fb_auto_account=fb_account_003&fb_auto_jsonl=${encodeURIComponent(firstFile)}&fb_auto_port=5012`, tabOne);
const two = config(`${facebook}?fb_auto_account=fb_account_003&fb_auto_jsonl=${encodeURIComponent(secondFile)}&fb_auto_port=5013`, tabTwo);
assert.equal(one.streamId, "fb_account_003_live_01");
assert.equal(two.streamId, "fb_account_003_live_02");
assert.equal(config(facebook, tabOne).jsonlFile, firstFile);
assert.equal(config(facebook, tabTwo).jsonlFile, secondFile);
assert.equal(config(facebook, tabOne).llmPort, 5012);
assert.equal(config(facebook, tabTwo).llmPort, 5013);
assert.equal(config(`${facebook}?fb_auto_account=fb_account_004&fb_auto_jsonl=${encodeURIComponent(firstFile)}`, storage()).llmPort, 5017);
assert.equal(config(`${facebook}#fb_auto_account=fb_account_003`, tabOne).jsonlFile, "");
assert.equal(config(facebook, tabTwo).jsonlFile, secondFile);

async function testReplyRouting() {
  const posted = [];
  let requestBody;
  const context = vm.createContext({
    window: { location: new URL(`${facebook}?fb_auto_account=fb_account_003&fb_auto_jsonl=${encodeURIComponent(firstFile)}&fb_auto_port=5012`) },
    sessionStorage: storage(),
    URLSearchParams,
    console: { log() {}, warn() {} },
    setInterval() {},
    setTimeout() {},
    fetch: async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return {
        ok: true,
        json: async () => ({ results: [
          { stream_id: "fb_account_003_live_02", raw_text: "別場直播", account_results: [{ account_id: "fb_account_003", reply: "WRONG" }] },
          { stream_id: "fb_account_003_live_01", raw_text: "本場直播", account_results: [{ account_id: "fb_account_003", reply: "RIGHT" }] }
        ] })
      };
    }
  });
  vm.runInContext(source, context);
  context.postComment = (reply) => { posted.push(reply); return true; };
  await vm.runInContext("pollReplies()", context);
  assert.equal(requestBody.stream_id, "fb_account_003_live_01");
  assert.equal(requestBody.file_path, firstFile);
  assert.deepEqual(posted, ["RIGHT"]);
}

testReplyRouting().then(
  () => console.log("Per-tab configuration and reply routing stay isolated."),
  (error) => { console.error(error); process.exitCode = 1; }
);
