"use client";

import { useState, useRef, useCallback, useEffect } from "react";

interface DownloadTask {
  taskId: string;
  url: string;
  state: string;
  percent: number;
  filename: string;
  title: string;
  error: string;
  speed: number | null;
  eta: number | null;
}

export default function Home() {
  const [url, setUrl] = useState("");
  const [tvCompat, setTvCompat] = useState(true);
  const [audioOnly, setAudioOnly] = useState(false);
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [serverStatus, setServerStatus] = useState<string>("checking...");
  const pollRef = useRef<NodeJS.Timeout | null>(null);
  const polledTasksRef = useRef<Set<string>>(new Set());

  // Check server health on mount
  useEffect(() => {
    async function checkServer() {
      try {
        const res = await fetch("/api/health");
        const data = await res.json();
        setServerStatus(
          `Connected — ${data.aria2c ? "aria2c ✓ (16 connections)" : "native downloader (16 fragments)"}, saves to ${data.downloads_folder}`
        );
      } catch {
        setServerStatus("Server not running — start the Python backend first");
      }
    }
    checkServer();
  }, []);

  // Poll progress for a specific task
  const pollTask = useCallback((taskId: string) => {
    if (polledTasksRef.current.has(taskId)) return;
    polledTasksRef.current.add(taskId);
  }, []);

  // Global poller — polls all active tasks every 2 seconds
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current);

    pollRef.current = setInterval(async () => {
      // Get all task IDs that are still active
      const activeIds = tasks
        .filter((t) => t.state !== "completed" && t.state !== "error" && t.taskId)
        .map((t) => t.taskId);

      if (activeIds.length === 0) return;

      for (const taskId of activeIds) {
        try {
          const res = await fetch(`/api/progress/${taskId}`);
          const data = await res.json();

          setTasks((prev) =>
            prev.map((t) =>
              t.taskId === taskId
                ? {
                    ...t,
                    state: data.state,
                    percent: data.percent || 0,
                    filename: data.filename || t.filename,
                    title: data.title || t.title,
                    error: data.error || "",
                    speed: data.speed || null,
                    eta: data.eta || null,
                  }
                : t
            )
          );
        } catch {
          // Server might be down
        }
      }
    }, 2000);

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [tasks]);

  const handleDownload = async () => {
    const currentUrl = url.trim();
    if (!currentUrl) return;

    // Prevent duplicate rows: a repeated submit of a URL that is already
    // downloading/queued would otherwise render one bar per submit.
    const alreadyActive = tasks.some(
      (t) => t.url === currentUrl && t.state !== "completed" && t.state !== "error"
    );
    if (alreadyActive) {
      setUrl("");
      return;
    }

    const newTask: DownloadTask = {
      taskId: "",
      url: currentUrl,
      state: "starting",
      percent: 0,
      filename: "",
      title: "",
      error: "",
      speed: null,
      eta: null,
    };

    setTasks((prev) => [...prev, newTask]);
    setUrl("");

    try {
      const res = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: currentUrl, tv_compat: tvCompat, audio_only: audioOnly }),
      });

      const data = await res.json();

      if (data.error) {
        setTasks((prev) =>
          prev.map((t) =>
            t.url === currentUrl && t.taskId === ""
              ? { ...t, state: "error", error: data.error }
              : t
          )
        );
        return;
      }

      // Update the task with the real task_id
      setTasks((prev) =>
        prev.map((t) =>
          t.url === currentUrl && t.taskId === ""
            ? { ...t, taskId: data.task_id, state: data.status === "started" ? "downloading" : "queued" }
            : t
        )
      );

      pollTask(data.task_id);
    } catch {
      setTasks((prev) =>
        prev.map((t) =>
          t.url === currentUrl && t.taskId === ""
            ? {
                ...t,
                state: "error",
                error: "Failed to connect to server. Is the Python backend running?",
              }
            : t
        )
      );
    }
  };

  const formatSpeed = (bytes: number | null) => {
    if (!bytes) return "";
    if (bytes > 1048576) return `${(bytes / 1048576).toFixed(1)} MB/s`;
    return `${(bytes / 1024).toFixed(0)} KB/s`;
  };

  const formatEta = (seconds: number | null) => {
    if (!seconds) return "";
    if (seconds > 60) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
    return `${seconds}s`;
  };

  const activeTasks = tasks.filter(
    (t) => t.state !== "completed" && t.state !== "error"
  );
  const doneTasks = tasks.filter(
    (t) => t.state === "completed" || t.state === "error"
  );

  const stateLabel = (s: string) => {
    if (s === "queued") return "⏳ queued";
    if (s === "starting") return "🔄 starting";
    if (s === "downloading") return "⬇️ downloading";
    if (s === "processing") return "⚙️ processing";
    if (s === "completed") return "✅ done";
    if (s === "error") return "❌ error";
    return s;
  };

  return (
    <div className="container">
      <h1>⬇️ Video Downloader</h1>
      <p className="subtitle">
        Paste a link from YouTube, Instagram, TikTok, Twitter, and 1000+ sites
      </p>

      {/* Server status */}
      <div className="card" style={{ padding: "10px 16px", fontSize: "0.8rem", marginBottom: "24px" }}>
        <span style={{ color: serverStatus.includes("Connected") ? "#4caf50" : "#ffc107" }}>
          ● {serverStatus}
        </span>
      </div>

      {/* URL input — always enabled so user can queue multiple */}
      <div className="input-group">
        <input
          type="text"
          placeholder="Paste video URL here..."
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && handleDownload()}
        />
        <button
          className="btn btn-primary"
          onClick={handleDownload}
          disabled={!url.trim()}
        >
          {activeTasks.length > 0 ? "Add to Queue" : "Download"}
        </button>
      </div>

      {/* Download options */}
      <div style={{ marginTop: "16px", display: "flex", flexDirection: "column", gap: "8px" }}>
        {/* Audio-only toggle */}
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: "8px",
            fontSize: "0.9rem",
            color: audioOnly ? "#4caf50" : "#999",
            cursor: "pointer",
            fontWeight: audioOnly ? 600 : 400,
          }}
        >
          <input
            type="checkbox"
            checked={audioOnly}
            onChange={(e) => {
              setAudioOnly(e.target.checked);
              if (e.target.checked) {
                setTvCompat(false); // Disable TV-compat when audio-only is selected
              }
            }}
          />
          🎵 Audio only (download as M4A audio file)
        </label>

        {/* TV-compatible toggle */}
        {!audioOnly && (
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: "8px",
              fontSize: "0.85rem",
              color: "#999",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              checked={tvCompat}
              onChange={(e) => setTvCompat(e.target.checked)}
            />
            TV-compatible format (H.264 · max 1080p) — plays on LCD/TV via USB
          </label>
        )}
      </div>

      {/* Active downloads */}
      {activeTasks.length > 0 && (
        <div className="card">
          <h3 style={{ marginBottom: "12px", fontSize: "1rem" }}>
            ⬇️ Active ({activeTasks.length})
          </h3>
          {activeTasks.map((task, i) => (
            <div key={task.taskId || i} style={{ marginBottom: i < activeTasks.length - 1 ? "16px" : 0 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "8px" }}>
                <span style={{ fontWeight: 600 }}>
                  {task.title || task.url}
                </span>
                <span className={`status-badge status-${task.state}`}>
                  {stateLabel(task.state)}
                </span>
              </div>

              <div className="progress-container">
                <div className="progress-bar-bg">
                  <div
                    className="progress-bar"
                    style={{ width: `${task.percent}%` }}
                  />
                </div>
                <div className="progress-info">
                  <span>{task.percent.toFixed(1)}%</span>
                  {task.speed && <span>{formatSpeed(task.speed)}</span>}
                  {task.eta && <span>ETA {formatEta(task.eta)}</span>}
                  {task.filename && <span>{task.filename}</span>}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Completed / Error tasks */}
      {doneTasks.length > 0 && (
        <div className="card">
          <h3 style={{ marginBottom: "12px", fontSize: "1rem" }}>📋 History</h3>
          <ul className="history-list">
            {doneTasks.map((item, i) => (
              <li key={item.taskId || i} style={{ display: "flex", justifyContent: "space-between" }}>
                <span>{item.title || item.filename || item.url}</span>
                <span className={`status-badge status-${item.state}`}>
                  {stateLabel(item.state)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Supported sites hint */}
      <div style={{ textAlign: "center", marginTop: "40px", color: "#555", fontSize: "0.8rem" }}>
        Powered by yt-dlp — supports YouTube, Instagram, TikTok, Twitter, Reddit, Vimeo, and 1000+ sites
      </div>
    </div>
  );
}
