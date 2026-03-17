const ports = new Set();
const uploads = new Map();

let config = null;
let csrfToken = "";
let processing = false;

function nowIso() {
    return new Date().toISOString();
}

function setUploadState(upload, changes) {
    Object.assign(upload, changes, {updatedAt: nowIso()});
}

function note(upload, message) {
    upload.message = message;
    upload.updatedAt = nowIso();
}

function serializeUpload(upload) {
    const progressPercent = upload.fileSize > 0 ? Math.min(100, Math.round((upload.uploadedBytes / upload.fileSize) * 100)) : 0;
    return {
        id: upload.id,
        title: upload.title,
        fileName: upload.fileName,
        fileSize: upload.fileSize,
        uploadedBytes: upload.uploadedBytes,
        sessionId: upload.sessionId,
        detailUrl: upload.detailUrl,
        assetId: upload.assetId,
        status: upload.status,
        message: upload.message,
        errorMessage: upload.errorMessage,
        chunkSize: upload.chunkSize,
        createdAt: upload.createdAt,
        updatedAt: upload.updatedAt,
        finishedAt: upload.finishedAt,
        progressPercent,
    };
}

function snapshot() {
    return Array.from(uploads.values())
        .sort((left, right) => String(right.createdAt).localeCompare(String(left.createdAt)))
        .map(serializeUpload);
}

function broadcast() {
    const payload = {type: "snapshot", uploads: snapshot()};
    for (const port of ports) {
        port.postMessage(payload);
    }
}

function sessionUrl(template, sessionId) {
    return template.replace(config.sessionIdPlaceholder, sessionId);
}

async function apiJson(url, options = {}) {
    const response = await fetch(url, {
        credentials: "same-origin",
        redirect: "follow",
        ...options,
        headers: {
            "X-CSRFToken": csrfToken,
            ...(options.headers || {}),
        },
    });
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("application/json")) {
        throw new Error("Session expired or server returned a non-JSON response.");
    }
    const payload = await response.json();
    if (!response.ok) {
        throw new Error(payload.error || `Request failed with status ${response.status}`);
    }
    return payload;
}

async function syncServerOffset(upload) {
    if (!upload.sessionId) {
        return upload.uploadedBytes;
    }
    const status = await apiJson(sessionUrl(config.uploadStatusUrlTemplate, upload.sessionId));
    const uploadedBytes = Number(status.uploaded_bytes || 0);
    setUploadState(upload, {uploadedBytes});
    return uploadedBytes;
}

async function ensureSession(upload) {
    if (upload.sessionId) {
        await syncServerOffset(upload);
        return;
    }
    note(upload, "Creating upload session");
    setUploadState(upload, {status: "starting", errorMessage: ""});
    broadcast();

    const init = await apiJson(config.uploadInitUrl, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            file_name: upload.fileName,
            file_size: upload.fileSize,
            content_type: upload.contentType,
            last_modified: String(upload.lastModified || ""),
        }),
    });
    setUploadState(upload, {
        sessionId: init.session_id,
        uploadedBytes: Number(init.uploaded_bytes || 0),
        chunkSize: Number(init.chunk_size || upload.chunkSize || 4 * 1024 * 1024),
        status: "uploading",
    });
    note(upload, "Upload session ready");
    broadcast();
}

async function uploadChunks(upload) {
    note(upload, "Uploading in background");
    setUploadState(upload, {status: "uploading"});
    broadcast();

    while (upload.uploadedBytes < upload.fileSize) {
        const chunkSize = upload.chunkSize || 4 * 1024 * 1024;
        const chunk = upload.file.slice(upload.uploadedBytes, upload.uploadedBytes + chunkSize);
        try {
            const payload = await apiJson(sessionUrl(config.uploadChunkUrlTemplate, upload.sessionId), {
                method: "PUT",
                headers: {
                    "Content-Type": "application/octet-stream",
                    "X-Chunk-Offset": String(upload.uploadedBytes),
                },
                body: chunk,
            });
            setUploadState(upload, {uploadedBytes: Number(payload.uploaded_bytes || upload.uploadedBytes + chunk.size)});
            note(upload, "Uploading in background");
        } catch (error) {
            if (String(error.message || "").includes("Offset mismatch")) {
                await syncServerOffset(upload);
                note(upload, "Resynced with server");
            } else {
                throw error;
            }
        }
        broadcast();
    }
}

async function finalizeUpload(upload) {
    setUploadState(upload, {status: "finalizing"});
    note(upload, "Finalizing asset");
    broadcast();

    const payload = await apiJson(sessionUrl(config.uploadCompleteUrlTemplate, upload.sessionId), {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({title: upload.title || ""}),
    });
    setUploadState(upload, {
        status: "complete",
        assetId: payload.asset_id || null,
        detailUrl: payload.detail_url || null,
        finishedAt: nowIso(),
    });
    note(upload, "Upload finished");
    upload.file = null;
    broadcast();
}

function nextPendingUpload() {
    return Array.from(uploads.values()).find((upload) => ["queued", "starting", "uploading", "finalizing"].includes(upload.status));
}

async function processQueue() {
    if (processing || !config) {
        return;
    }
    processing = true;
    try {
        while (true) {
            const upload = nextPendingUpload();
            if (!upload) {
                break;
            }
            try {
                await ensureSession(upload);
                await uploadChunks(upload);
                await finalizeUpload(upload);
            } catch (error) {
                setUploadState(upload, {
                    status: "failed",
                    errorMessage: String(error.message || error),
                    finishedAt: nowIso(),
                });
                note(upload, upload.errorMessage);
                broadcast();
            }
        }
    } finally {
        processing = false;
    }
}

function enqueueUpload(data) {
    const upload = {
        id: data.id,
        title: data.title || "",
        file: data.file,
        fileName: data.file.name,
        fileSize: data.file.size,
        contentType: data.file.type || "application/octet-stream",
        lastModified: data.file.lastModified || 0,
        sessionId: null,
        assetId: null,
        detailUrl: null,
        uploadedBytes: 0,
        chunkSize: 4 * 1024 * 1024,
        status: "queued",
        message: "Queued",
        errorMessage: "",
        createdAt: nowIso(),
        updatedAt: nowIso(),
        finishedAt: null,
    };
    uploads.set(upload.id, upload);
    broadcast();
    processQueue();
}

function retryUpload(id) {
    const upload = uploads.get(id);
    if (!upload || upload.status !== "failed") {
        return;
    }
    setUploadState(upload, {
        status: "queued",
        errorMessage: "",
        finishedAt: null,
    });
    note(upload, "Queued for retry");
    broadcast();
    processQueue();
}

function dismissUpload(id) {
    const upload = uploads.get(id);
    if (!upload || !["failed", "complete"].includes(upload.status)) {
        return;
    }
    uploads.delete(id);
    broadcast();
}

self.onconnect = (event) => {
    const port = event.ports[0];
    ports.add(port);

    port.onmessage = (messageEvent) => {
        const payload = messageEvent.data || {};
        switch (payload.type) {
            case "configure":
                config = payload.config || config;
                csrfToken = payload.csrfToken || csrfToken;
                port.postMessage({type: "snapshot", uploads: snapshot()});
                processQueue();
                break;
            case "snapshot":
                port.postMessage({type: "snapshot", uploads: snapshot()});
                break;
            case "enqueue":
                if (payload.file) {
                    enqueueUpload(payload);
                }
                break;
            case "retry":
                retryUpload(payload.id);
                break;
            case "dismiss":
                dismissUpload(payload.id);
                break;
            case "disconnect":
                ports.delete(port);
                break;
            default:
                break;
        }
    };

    port.start();
    port.postMessage({type: "snapshot", uploads: snapshot()});
};
