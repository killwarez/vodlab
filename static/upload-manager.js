(function () {
    const config = window.MEDIA_HUB_UPLOAD_CONFIG;
    if (!config) {
        return;
    }

    const state = {
        uploads: [],
        trayOpen: sessionStorage.getItem("upload-tray-open") !== "0",
        unsupported: false,
    };

    let port = null;
    let softNavigationInFlight = false;

    function makeUploadId() {
        return (window.crypto && window.crypto.randomUUID)
            ? window.crypto.randomUUID()
            : `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    }

    function escapeHtml(value) {
        return String(value ?? "")
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#39;");
    }

    function formatBytes(value) {
        const size = Number(value || 0);
        if (!size) {
            return "0 B";
        }
        const units = ["B", "KB", "MB", "GB", "TB"];
        let current = size;
        let unit = units[0];
        for (unit of units) {
            if (current < 1024 || unit === units[units.length - 1]) {
                break;
            }
            current /= 1024;
        }
        const decimals = unit === "B" ? 0 : 1;
        return `${current.toFixed(decimals)} ${unit}`;
    }

    function statusLabel(status) {
        return {
            queued: "Queued",
            starting: "Starting",
            uploading: "Uploading",
            finalizing: "Finalizing",
            complete: "Complete",
            failed: "Failed",
        }[status] || "Unknown";
    }

    function statusTone(status) {
        return {
            queued: "tone-warning",
            starting: "tone-info",
            uploading: "tone-info",
            finalizing: "tone-info",
            complete: "tone-success",
            failed: "tone-danger",
        }[status] || "tone-info";
    }

    function isTerminal(upload) {
        return ["complete", "failed"].includes(upload.status);
    }

    function activeUploads() {
        return state.uploads.filter((upload) => !isTerminal(upload));
    }

    function completedUploads() {
        return state.uploads.filter((upload) => isTerminal(upload));
    }

    function uploadActionMarkup(upload) {
        const detailButton = upload.detailUrl
            ? `<a class="btn btn-sm btn-outline-primary" href="${escapeHtml(upload.detailUrl)}">Open</a>`
            : "";
        const retryButton = upload.status === "failed"
            ? `<button class="btn btn-sm btn-outline-primary" type="button" data-upload-action="retry" data-upload-id="${escapeHtml(upload.id)}">Retry</button>`
            : "";
        const dismissButton = isTerminal(upload)
            ? `<button class="btn btn-sm btn-outline-danger" type="button" data-upload-action="dismiss" data-upload-id="${escapeHtml(upload.id)}">Dismiss</button>`
            : "";
        return `${detailButton}${retryButton}${dismissButton}`;
    }

    function uploadCardMarkup(upload, compact) {
        const progressPercent = Number(upload.progressPercent || 0);
        const sizeSummary = `${formatBytes(upload.uploadedBytes)} of ${formatBytes(upload.fileSize)}`;
        return `
            <article class="upload-live-card ${compact ? "upload-live-card-compact" : ""}">
                <div class="upload-live-head">
                    <div>
                        <div class="upload-live-title">${escapeHtml(upload.title || upload.fileName)}</div>
                        <div class="upload-live-file">${escapeHtml(upload.fileName)}</div>
                    </div>
                    <span class="upload-live-badge ${statusTone(upload.status)}">${statusLabel(upload.status)}</span>
                </div>
                <div class="upload-live-progress-shell" aria-hidden="true">
                    <div class="upload-live-progress-bar" style="width: ${progressPercent}%"></div>
                </div>
                <div class="upload-live-meta">
                    <span>${progressPercent}%</span>
                    <span>${sizeSummary}</span>
                </div>
                <div class="upload-live-message">${escapeHtml(upload.errorMessage || upload.message || "")}</div>
                <div class="upload-live-actions">
                    ${uploadActionMarkup(upload)}
                </div>
            </article>
        `;
    }

    function trayElements() {
        return {
            root: document.getElementById("upload-tray"),
            toggle: document.getElementById("upload-tray-toggle"),
            count: document.getElementById("upload-tray-count"),
            panel: document.getElementById("upload-tray-panel"),
            list: document.getElementById("upload-tray-list"),
        };
    }

    function pageElements() {
        return {
            button: document.getElementById("upload-button"),
            fileInput: document.getElementById("upload-file"),
            titleInput: document.getElementById("upload-title"),
            status: document.getElementById("upload-status"),
            empty: document.getElementById("upload-page-empty"),
            list: document.getElementById("upload-page-list"),
        };
    }

    function renderTray() {
        const {root, count, panel, list} = trayElements();
        if (!root || !count || !panel || !list) {
            return;
        }

        if (!state.uploads.length) {
            root.classList.add("upload-tray-hidden");
            panel.hidden = true;
            list.innerHTML = "";
            count.textContent = "0";
            return;
        }

        root.classList.remove("upload-tray-hidden");
        panel.hidden = !state.trayOpen;
        const activeCount = activeUploads().length;
        count.textContent = String(activeCount || completedUploads().length);
        list.innerHTML = state.uploads.map((upload) => uploadCardMarkup(upload, true)).join("");
    }

    function renderUploadPage() {
        const {status, empty, list, button} = pageElements();
        if (!status || !empty || !list) {
            return;
        }

        if (state.unsupported) {
            status.textContent = "Background uploads are not supported in this browser.";
            empty.hidden = false;
            list.innerHTML = "";
            if (button) {
                button.disabled = true;
            }
            return;
        }

        if (!state.uploads.length) {
            status.textContent = "No active uploads.";
            empty.hidden = false;
            list.innerHTML = "";
            return;
        }

        const activeCount = activeUploads().length;
        status.textContent = activeCount > 0
            ? `${activeCount} upload${activeCount === 1 ? "" : "s"} in progress`
            : "All uploads finished";
        empty.hidden = true;
        list.innerHTML = state.uploads.map((upload) => uploadCardMarkup(upload, false)).join("");
    }

    function renderAll() {
        renderTray();
        renderUploadPage();
    }

    function handleWorkerMessage(event) {
        const payload = event.data || {};
        if (payload.type === "snapshot") {
            state.uploads = Array.isArray(payload.uploads) ? payload.uploads : [];
            renderAll();
        }
    }

    function connectWorker() {
        if (!window.SharedWorker) {
            state.unsupported = true;
            renderAll();
            return;
        }

        const worker = new SharedWorker(config.workerUrl);
        port = worker.port;
        port.onmessage = handleWorkerMessage;
        port.start();
        port.postMessage({
            type: "configure",
            config,
            csrfToken: document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "",
        });
        port.postMessage({type: "snapshot"});
    }

    function enqueueFromUploadPage() {
        const {fileInput, titleInput, status} = pageElements();
        if (!fileInput || !status) {
            return;
        }
        if (state.unsupported) {
            status.textContent = "Background uploads are not supported in this browser.";
            return;
        }
        if (!port) {
            status.textContent = "Upload manager is still starting. Try again in a moment.";
            return;
        }

        const files = Array.from(fileInput.files || []);
        if (!files.length) {
            status.textContent = "Choose one or more files before starting the upload.";
            return;
        }

        const customTitle = titleInput ? titleInput.value.trim() : "";
        const singleUpload = files.length === 1;

        for (const file of files) {
            port.postMessage({
                type: "enqueue",
                id: makeUploadId(),
                title: singleUpload ? customTitle : "",
                file,
            });
        }

        fileInput.value = "";
        if (titleInput) {
            titleInput.value = "";
        }
        if (singleUpload) {
            status.textContent = "Upload queued.";
            return;
        }
        if (customTitle) {
            status.textContent = `${files.length} uploads queued. Custom title was ignored because multiple files were selected.`;
            return;
        }
        status.textContent = `${files.length} uploads queued.`;
    }

    function bindTray() {
        const {toggle, panel} = trayElements();
        if (toggle && panel) {
            toggle.addEventListener("click", () => {
                state.trayOpen = !state.trayOpen;
                sessionStorage.setItem("upload-tray-open", state.trayOpen ? "1" : "0");
                panel.hidden = !state.trayOpen;
            });
        }
    }

    function handleActionClick(event) {
        const actionButton = event.target.closest("[data-upload-action]");
        if (!actionButton || !port) {
            return;
        }
        const action = actionButton.getAttribute("data-upload-action");
        const uploadId = actionButton.getAttribute("data-upload-id");
        if (!action || !uploadId) {
            return;
        }
        if (action === "retry") {
            port.postMessage({type: "retry", id: uploadId});
        }
        if (action === "dismiss") {
            port.postMessage({type: "dismiss", id: uploadId});
        }
    }

    function hasActiveUploads() {
        return activeUploads().length > 0;
    }

    function shouldSoftNavigate(anchor, event) {
        if (!anchor || !hasActiveUploads() || softNavigationInFlight) {
            return false;
        }
        if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
            return false;
        }
        if (anchor.target && anchor.target !== "_self") {
            return false;
        }
        if (anchor.hasAttribute("download") || anchor.getAttribute("href") === "#") {
            return false;
        }
        const href = anchor.getAttribute("href") || "";
        if (!href || href.startsWith("#") || href.startsWith("mailto:") || href.startsWith("tel:") || href.startsWith("javascript:")) {
            return false;
        }
        const targetUrl = new URL(anchor.href, window.location.href);
        if (targetUrl.origin !== window.location.origin) {
            return false;
        }
        return true;
    }

    function executeScripts(root) {
        root.querySelectorAll("script").forEach((script) => {
            const replacement = document.createElement("script");
            for (const attribute of script.attributes) {
                replacement.setAttribute(attribute.name, attribute.value);
            }
            if (!script.src) {
                replacement.textContent = script.textContent;
            }
            script.replaceWith(replacement);
        });
    }

    async function softNavigate(url, options = {}) {
        const {replace = false} = options;
        const current = document.getElementById("app-content");
        const currentScripts = document.getElementById("page-extra-scripts");
        if (!current) {
            window.location.href = url;
            return;
        }

        softNavigationInFlight = true;
        current.classList.add("soft-nav-loading");
        try {
            const response = await fetch(url, {
                credentials: "same-origin",
                headers: {"X-Requested-With": "MediaHubSoftNavigation"},
            });
            const html = await response.text();
            const nextDocument = new DOMParser().parseFromString(html, "text/html");
            const nextContent = nextDocument.getElementById("app-content");
            const nextScripts = nextDocument.getElementById("page-extra-scripts");
            if (!nextContent) {
                window.location.href = url;
                return;
            }

            current.replaceWith(nextContent);
            if (currentScripts && nextScripts) {
                currentScripts.replaceWith(nextScripts);
            }
            document.title = nextDocument.title || document.title;
            if (window.htmx) {
                window.htmx.process(nextContent);
            }
            executeScripts(nextContent);
            if (nextScripts) {
                executeScripts(nextScripts);
            }
            renderAll();
            window.scrollTo({top: 0, behavior: "auto"});
            if (replace) {
                window.history.replaceState({soft: true}, "", url);
            } else {
                window.history.pushState({soft: true}, "", url);
            }
        } finally {
            softNavigationInFlight = false;
            const content = document.getElementById("app-content");
            if (content) {
                content.classList.remove("soft-nav-loading");
            }
        }
    }

    function bindDocumentEvents() {
        document.addEventListener("click", (event) => {
            const actionButton = event.target.closest("[data-upload-action]");
            if (actionButton) {
                handleActionClick(event);
                return;
            }

            const uploadButton = event.target.closest("#upload-button");
            if (uploadButton) {
                event.preventDefault();
                enqueueFromUploadPage();
                return;
            }

            const anchor = event.target.closest("a[href]");
            if (!shouldSoftNavigate(anchor, event)) {
                return;
            }
            event.preventDefault();
            softNavigate(anchor.href);
        });

        window.addEventListener("popstate", () => {
            if (!hasActiveUploads()) {
                return;
            }
            softNavigate(window.location.href, {replace: true});
        });

        window.addEventListener("beforeunload", (event) => {
            if (!hasActiveUploads()) {
                return;
            }
            event.preventDefault();
            event.returnValue = "";
        });
    }

    document.addEventListener("DOMContentLoaded", () => {
        window.history.replaceState({soft: true}, "", window.location.href);
        bindTray();
        bindDocumentEvents();
        connectWorker();
        renderAll();
    });

    window.MediaHubNavigate = function (url) {
        if (!url) {
            return;
        }
        if (hasActiveUploads()) {
            softNavigate(url);
            return;
        }
        window.location.href = url;
    };
})();
