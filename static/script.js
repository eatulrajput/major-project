// static/js/script.js
(() => {
  // ==== Helpers ====
  const byId = (id) => document.getElementById(id);
  const chatBox = byId("chat-box");
  const messageInput = byId("message");
  const pdfInput = byId("pdf-upload");
  const uploadStatus = byId("upload-status");

  // Guard: only run on the chat page that has our elements
  if (!chatBox || !messageInput) return;

  // ==== Chat UI ====
  async function sendMessage() {
    const message = messageInput.value.trim();
    if (!message) return;

    // Add user message bubble
    const user = document.createElement("div");
    user.className = "bg-blue-600 p-2 rounded-lg self-end max-w-lg";
    user.textContent = "👤 " + message;
    chatBox.appendChild(user);
    chatBox.scrollTop = chatBox.scrollHeight;
    messageInput.value = "";

    // Typing indicator
    const typing = document.createElement("div");
    typing.className = "bg-gray-700 p-2 rounded-lg self-start max-w-lg italic opacity-80";
    typing.textContent = "🤖 typing…";
    chatBox.appendChild(typing);
    chatBox.scrollTop = chatBox.scrollHeight;

    try {
      const res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message })
      });
      const data = await res.json();
      typing.remove();

      const bot = document.createElement("div");
      bot.className = "bg-gray-700 p-2 rounded-lg self-start max-w-lg whitespace-pre-wrap";
      bot.textContent = "🤖 " + (data.reply || "No response.");
      chatBox.appendChild(bot);
      chatBox.scrollTop = chatBox.scrollHeight;
    } catch (e) {
      typing.remove();
      const bot = document.createElement("div");
      bot.className = "bg-gray-700 p-2 rounded-lg self-start max-w-lg";
      bot.textContent = "🤖 Network error.";
      chatBox.appendChild(bot);
    }
  }

  // Enter to send
  messageInput.addEventListener("keypress", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendMessage();
    }
  });
  // If you have a send button with id="send-btn", hook it:
  const sendBtn = byId("send-btn");
  if (sendBtn) sendBtn.addEventListener("click", sendMessage);

  // ==== PDF Upload ====
  async function uploadPDF() {
    if (!pdfInput.files || pdfInput.files.length === 0) {
      alert("Please select a PDF file!");
      return;
    }
    uploadStatus.textContent = "Uploading…";
    const formData = new FormData();
    formData.append("file", pdfInput.files[0]);

    try {
      const res = await fetch("/upload", { method: "POST", body: formData });
      const data = await res.json();
      uploadStatus.textContent = res.ok ? "✅ PDF Uploaded Successfully!" : ("❌ " + (data.message || "Upload failed"));
    } catch {
      uploadStatus.textContent = "❌ Upload failed (network).";
    }
  }
  pdfInput?.addEventListener("change", uploadPDF);

  // ==== Scraper Controls ====
  const stUrl   = byId("scrape-start-url");
  const stMax   = byId("scrape-max-pages");
  const stDelay = byId("scrape-delay");
  const stBox   = byId("scrape-status");

  function setScrapeStatus(obj) {
    if (!stBox) return;
    stBox.textContent = typeof obj === "string" ? obj : JSON.stringify(obj, null, 2);
  }

  async function startScrape() {
    if (!stUrl || !stMax || !stDelay) return;
    setScrapeStatus("⏳ Starting scraper...");
    try {
      const res = await fetch("/scrape/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          start_url: stUrl.value.trim(),
          max_pages: parseInt(stMax.value, 10),
          delay: parseFloat(stDelay.value)
        })
      });
      setScrapeStatus(await res.json());
    } catch (e) {
      setScrapeStatus({ error: "Failed to start scraper", detail: String(e) });
    }
  }

  async function checkScrapeStatus() {
    try {
      const res = await fetch("/scrape/status");
      setScrapeStatus(await res.json());
    } catch (e) {
      setScrapeStatus({ error: "Failed to fetch status", detail: String(e) });
    }
  }

  async function rebuildIndex() {
    setScrapeStatus("🔄 Rebuilding TF-IDF index...");
    try {
      const res = await fetch("/reindex", { method: "POST" });
      setScrapeStatus(await res.json());
    } catch (e) {
      setScrapeStatus({ error: "Failed to rebuild index", detail: String(e) });
    }
  }

  async function stopScrape() {
    try {
      const res = await fetch("/scrape/stop", { method: "POST" });
      setScrapeStatus(await res.json());
    } catch (e) {
      setScrapeStatus({ error: "Stop endpoint not available", detail: String(e) });
    }
  }

  // Hook scraper buttons if present
  const btnStart  = document.getElementById("btn-start");
  const btnStatus = document.getElementById("btn-status");
  const btnReidx  = document.getElementById("btn-reindex");
  const btnStop   = document.getElementById("btn-stop");

  if (btnStart)  btnStart.addEventListener("click", startScrape);
  if (btnStatus) btnStatus.addEventListener("click", checkScrapeStatus);
  if (btnReidx)  btnReidx.addEventListener("click", rebuildIndex);
  if (btnStop)   btnStop.addEventListener("click", stopScrape);

  // Also expose globally if your HTML uses onclick=""
  window.sendMessage = sendMessage;
  window.startScrape = startScrape;
  window.checkScrapeStatus = checkScrapeStatus;
  window.rebuildIndex = rebuildIndex;
  window.stopScrape = stopScrape;
})();
