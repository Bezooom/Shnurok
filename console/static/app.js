const $ = (id) => document.getElementById(id);
const state = {
  view: "overview",
  name: "",
  fs: { scope: "media", path: "/", bundle: "" },
  apps: [],
  kind: "User",
  log: [],
  socket: null,
  listening: false,
};

function bytes(n) {
  if (n == null || Number.isNaN(Number(n))) return "—";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${i ? v.toFixed(v >= 10 ? 0 : 1) : v} ${units[i]}`;
}

function toast(message) {
  const node = $("toast");
  if (!message) {
    node.hidden = true;
    node.textContent = "";
    return;
  }
  node.hidden = false;
  node.textContent = message;
}

async function api(url, options) {
  const response = await fetch(url, options);
  const type = response.headers.get("content-type") || "";
  if (!response.ok) {
    let message = response.statusText;
    if (type.includes("json")) {
      const body = await response.json();
      message = body.error || message;
    } else {
      message = (await response.text()) || message;
    }
    throw new Error(message);
  }
  if (type.includes("json")) return response.json();
  return response;
}

function jsonPost(url, body) {
  return api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function show(view) {
  state.view = view;
  for (const button of document.querySelectorAll(".tab")) {
    button.classList.toggle("is-on", button.dataset.view === view);
  }
  for (const section of document.querySelectorAll("main > section")) {
    section.hidden = section.id !== `view-${view}`;
  }
  if (view === "overview") loadOverview();
  if (view === "files") loadFiles();
  if (view === "apps") loadApps();
  if (view === "log") startLog();
  if (view === "copies") loadCopies();
  if (view === "speed") loadSpeed();
  if (view === "system") loadSystem();
  if (view !== "log") stopLog(false);
}

async function refreshStatus() {
  try {
    const data = await api("/api/status");
    const led = $("led");
    const mode = $("mode-label");
    led.classList.toggle("on", !!data.connected);
    if (!data.connected) {
      mode.textContent = "нет кабеля";
      $("device-name").textContent = "Нет айпада";
      $("device-sub").textContent = "Подключите его по USB и разблокируйте экран.";
      return;
    }
    const modes = {
      normal: "USB · обычный режим",
      recovery_mode_1: "recovery",
      recovery_mode_2: "recovery",
      recovery_mode_3: "recovery",
      recovery_mode_4: "recovery",
      dfu_mode: "DFU",
    };
    mode.textContent = modes[data.mode] || data.mode || "USB";
    state.name = data.name || state.name;
    $("device-name").textContent = data.name || "айпад";
    const bits = [data.product, data.version].filter(Boolean);
    $("device-sub").textContent = bits.join(" · ");
  } catch (error) {
    $("mode-label").textContent = "ошибка связи";
    toast(error.message);
  }
}

function row(label, value) {
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value == null || value === "" ? "—" : String(value);
  return [dt, dd];
}

async function loadOverview() {
  try {
    const data = await api("/api/overview");
    const id = data.identity || {};
    state.name = id.DeviceName || state.name;
    $("device-name").textContent = state.name || "айпад";
    $("device-sub").textContent = [id.ProductType, "iOS " + (id.ProductVersion || ""), id.BuildVersion]
      .filter((part) => part && part !== "iOS ")
      .join(" · ");
    const spec = $("spec");
    spec.replaceChildren();
    const fields = [
      ["Имя", id.DeviceName],
      ["Модель", id.ProductType],
      ["Железо", id.HardwareModel],
      ["Система", `${id.ProductVersion || "—"} (${id.BuildVersion || "—"})`],
      ["Серийный", id.SerialNumber],
      ["UDID", id.UniqueDeviceID],
      ["Активация", id.ActivationState],
      ["Код-пароль", id.PasswordProtected ? "включён" : "нет"],
      ["Язык", `${id.Language || "—"} / ${id.Locale || "—"}`],
      ["Часовой пояс", id.TimeZone],
      ["Время на айпаде", data.time],
      ["Wi-Fi", id.WiFiAddress],
      ["Bluetooth", id.BluetoothAddress],
      ["Регион", id.RegionInfo],
      ["iBoot", id.FirmwareVersion],
      ["Wi-Fi lockdown", data.wifi_lockdown ? "разрешён" : "только кабель"],
      ["Копия шифруется", data.backup_encrypted ? "да" : "нет"],
    ];
    for (const [label, value] of fields) spec.append(...row(label, value));
    $("sys-name").value = id.DeviceName || "";
    $("sys-lang").value = id.Language || "";
    $("sys-locale").value = id.Locale || "";
    $("sys-wifi").checked = !!data.wifi_lockdown;

    const battery = data.battery || {};
    const pct = Number(battery.BatteryCurrentCapacity ?? 0);
    $("batt-pct").textContent = `${pct}%`;
    $("batt-bar").style.width = `${Math.max(0, Math.min(100, pct))}%`;
    const charge = battery.BatteryIsCharging ? "заряжается" : "не заряжается";
    const plugged = battery.ExternalConnected ? "кабель подключён" : "от кабеля отключён";
    $("batt-fine").textContent = `${charge}, ${plugged}`;

    const disk = data.disk || {};
    const cap = Number(disk.TotalDataCapacity || disk.TotalDiskCapacity || 0);
    const free = Number(disk.AmountDataAvailable ?? disk.TotalDataAvailable ?? 0);
    const used = Math.max(0, cap - free);
    $("disk-free").textContent = bytes(free);
    $("disk-fine").textContent = `свободно из ${bytes(cap)}, занято ${bytes(used)}`;
    $("disk-bar").style.width = cap ? `${Math.round((used / cap) * 100)}%` : "0";

    const cell = data.cell || {};
    $("cell-health").textContent = cell.health_percent != null ? `${cell.health_percent}%` : "—";
    $("cell-fine").textContent = [
      cell.temperature_c != null ? `${cell.temperature_c} °C` : null,
      cell.cycles != null ? `${cell.cycles} циклов` : null,
      cell.full_mah ? `${cell.full_mah} / ${cell.design_mah} мА·ч` : null,
      cell.voltage_mv ? `${cell.voltage_mv} мВ` : null,
    ].filter(Boolean).join(" · ");

    const wall = $("wallpaper");
    if (!wall.dataset.loaded) {
      wall.src = "/api/wallpaper";
      wall.dataset.loaded = "1";
    }
  } catch (error) {
    toast(error.message);
  }
}

const JUMPS = [
  ["/", "Корень"],
  ["/DCIM", "Камера"],
  ["/Photos", "Фото"],
  ["/Downloads", "Загрузки"],
  ["/Books", "Книги"],
  ["/Recordings", "Записи"],
];

function renderJumps() {
  const host = $("fs-jumps");
  host.replaceChildren();
  if (state.fs.scope !== "media") {
    $("fs-scope-note").textContent = state.fs.bundle
      ? `Программа ${state.fs.bundle}`
      : "Эта область не привязана к программе. Откройте её из списка программ.";
    return;
  }
  $("fs-scope-note").textContent = "Медиатека: то, что видит компьютер. Системный раздел сюда не входит.";
  for (const [path, label] of JUMPS) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => {
      state.fs.path = path;
      loadFiles();
    });
    host.append(button);
  }
}

function renderCrumb() {
  const host = $("fs-crumb");
  host.replaceChildren();
  const parts = state.fs.path.split("/").filter(Boolean);
  const root = document.createElement("button");
  root.type = "button";
  root.textContent = "корень";
  root.addEventListener("click", () => {
    state.fs.path = "/";
    loadFiles();
  });
  host.append(root);
  let acc = "";
  for (const part of parts) {
    acc += `/${part}`;
    const target = acc;
    host.append(" / ");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = part;
    button.addEventListener("click", () => {
      state.fs.path = target;
      loadFiles();
    });
    host.append(button);
  }
}

function fileUrl(path, attachment) {
  const q = new URLSearchParams({
    scope: state.fs.scope,
    path,
    bundle: state.fs.bundle || "",
    attachment: attachment ? "1" : "0",
  });
  return `/api/fs/download?${q}`;
}

function preview(entry) {
  const host = $("fs-preview");
  host.replaceChildren();
  const title = document.createElement("h3");
  title.textContent = entry.name;
  host.append(title);
  const ext = (entry.name.split(".").pop() || "").toLowerCase();
  if (!entry.dir && ["jpg", "jpeg", "png", "gif", "webp", "heic"].includes(ext)) {
    const img = document.createElement("img");
    img.alt = entry.name;
    img.src = fileUrl(entry.path, false);
    host.append(img);
  } else if (!entry.dir && ["mov", "mp4", "m4v"].includes(ext)) {
    const video = document.createElement("video");
    video.controls = true;
    video.src = fileUrl(entry.path, false);
    host.append(video);
  } else if (!entry.dir && ["txt", "plist", "xml", "log", "json", "csv"].includes(ext) && (entry.size || 0) < 200000) {
    const pre = document.createElement("pre");
    pre.textContent = "читаю…";
    host.append(pre);
    fetch(fileUrl(entry.path, false))
      .then((response) => response.text())
      .then((text) => {
        pre.textContent = text.slice(0, 20000);
      })
      .catch((error) => {
        pre.textContent = error.message;
      });
  } else {
    const note = document.createElement("p");
    note.className = "fine";
    note.textContent = entry.dir ? "Папка" : bytes(entry.size);
    host.append(note);
  }
}

async function loadFiles() {
  renderJumps();
  renderCrumb();
  $("fs-scope").value = state.fs.scope;
  const body = $("fs-rows");
  body.replaceChildren();
  try {
    const data = await api(
      `/api/fs/list?${new URLSearchParams({
        scope: state.fs.scope,
        path: state.fs.path,
        bundle: state.fs.bundle || "",
      })}`,
    );
    if (!data.entries.length) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td colspan="4">Пусто</td>`;
      body.append(tr);
      return;
    }
    for (const entry of data.entries) {
      const tr = document.createElement("tr");
      const name = document.createElement("td");
      const open = document.createElement("button");
      open.type = "button";
      open.className = "name-btn";
      open.textContent = (entry.dir ? "▸ " : "") + entry.name;
      open.addEventListener("click", () => {
        if (entry.dir) {
          state.fs.path = entry.path;
          loadFiles();
        } else {
          preview(entry);
        }
      });
      name.append(open);
      const size = document.createElement("td");
      size.textContent = entry.dir ? "" : bytes(entry.size);
      const when = document.createElement("td");
      when.textContent = entry.mtime || "";
      const actions = document.createElement("td");
      actions.className = "row-actions";
      if (!entry.dir) {
        const save = document.createElement("a");
        save.href = fileUrl(entry.path, true);
        save.textContent = "скачать";
        actions.append(save);
      }
      const rename = document.createElement("button");
      rename.type = "button";
      rename.textContent = "имя";
      rename.addEventListener("click", async () => {
        const next = prompt("Новое имя", entry.name);
        if (!next || next === entry.name) return;
        const parent = state.fs.path === "/" ? "" : state.fs.path;
        try {
          await jsonPost("/api/fs/rename", {
            scope: state.fs.scope,
            path: entry.path,
            to: `${parent}/${next}`.replace("//", "/"),
            bundle: state.fs.bundle,
          });
          loadFiles();
        } catch (error) {
          toast(error.message);
        }
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "удалить";
      remove.addEventListener("click", async () => {
        if (!confirm(`Удалить ${entry.name}?`)) return;
        try {
          await jsonPost("/api/fs/delete", {
            scope: state.fs.scope,
            path: entry.path,
            bundle: state.fs.bundle,
          });
          loadFiles();
        } catch (error) {
          toast(error.message);
        }
      });
      actions.append(rename, remove);
      tr.append(name, size, when, actions);
      body.append(tr);
    }
  } catch (error) {
    toast(error.message);
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 4;
    td.textContent = error.message;
    tr.append(td);
    body.append(tr);
  }
}

function openAppFiles(app, scope) {
  state.fs.scope = scope;
  state.fs.bundle = app.id;
  state.fs.path = "/";
  show("files");
}

async function loadApps() {
  const host = $("app-list");
  host.textContent = "Смотрю список…";
  try {
    const data = await api(`/api/apps?kind=${state.kind}`);
    state.apps = data.apps || [];
    paintApps();
  } catch (error) {
    host.textContent = error.message;
    toast(error.message);
  }
}

function paintApps() {
  const host = $("app-list");
  const query = $("app-q").value.trim().toLowerCase();
  const rows = state.apps.filter((app) => {
    if (!query) return true;
    return `${app.name} ${app.id}`.toLowerCase().includes(query);
  });
  host.replaceChildren();
  if (!rows.length) {
    host.textContent = "Ничего не нашлось";
    return;
  }
  for (const app of rows) {
    const card = document.createElement("article");
    card.className = "app";
    const img = document.createElement("img");
    img.alt = "";
    img.src = `/api/apps/icon?bundle=${encodeURIComponent(app.id)}`;
    img.addEventListener("error", () => {
      img.removeAttribute("src");
    });
    const text = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = app.name;
    const meta = document.createElement("div");
    meta.className = "id";
    meta.textContent = `${app.version || "—"} · ${app.id}`;
    text.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "row-actions";
    if (app.type === "User") {
      const docs = document.createElement("button");
      docs.type = "button";
      docs.textContent = "Документы";
      docs.addEventListener("click", () => openAppFiles(app, "appdocs"));
      const box = document.createElement("button");
      box.type = "button";
      box.textContent = "Контейнер";
      box.addEventListener("click", () => openAppFiles(app, "appbox"));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "hot";
      remove.textContent = "Удалить";
      remove.addEventListener("click", async () => {
        if (!confirm(`Удалить «${app.name}» с айпада?`)) return;
        try {
          await jsonPost("/api/apps/uninstall", { bundle_id: app.id });
          loadApps();
        } catch (error) {
          toast(error.message);
        }
      });
      actions.append(docs, box, remove);
    }
    card.append(img, text, actions);
    host.append(card);
  }
}

function paintLog() {
  const query = $("log-q").value.trim().toLowerCase();
  const lines = query ? state.log.filter((line) => line.toLowerCase().includes(query)) : state.log;
  $("log-view").textContent = lines.slice(-400).join("\n");
  const view = $("log-view");
  view.scrollTop = view.scrollHeight;
}

function startLog() {
  if (state.socket) return;
  state.listening = true;
  $("log-toggle").textContent = "Пауза";
  const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/syslog`);
  state.socket = socket;
  socket.addEventListener("message", (event) => {
    state.log.push(event.data);
    if (state.log.length > 800) state.log.splice(0, state.log.length - 800);
    if (state.view === "log") paintLog();
  });
  socket.addEventListener("close", () => {
    state.socket = null;
    if (state.listening && state.view === "log") {
      setTimeout(startLog, 1500);
    } else {
      $("log-toggle").textContent = "Слушать";
    }
  });
}

function stopLog(manual) {
  if (manual) state.listening = false;
  if (state.socket) {
    state.socket.close();
    state.socket = null;
  }
  $("log-toggle").textContent = "Слушать";
}

async function loadCopies() {
  try {
    const [baks, relay] = await Promise.all([api("/api/backups"), api("/api/relay")]);
    const list = $("bak-list");
    list.replaceChildren();
    if (!baks.backups.length) {
      const li = document.createElement("li");
      li.textContent = "Копий на этом компьютере ещё нет";
      list.append(li);
    }
    for (const item of baks.backups) {
      const li = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = `${item.device || item.udid} · iOS ${item.version || "?"} · ${item.when || ""}`;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Вернуть на айпад";
      button.addEventListener("click", async () => {
        const confirmName = prompt(`Это заменит данные на айпаде. Введите имя «${state.name}»`);
        if (confirmName == null) return;
        try {
          await jsonPost("/api/backup/restore", {
            confirm: confirmName,
            password: $("bak-pass").value,
            udid: item.udid,
          });
          pollJobs();
        } catch (error) {
          toast(error.message);
        }
      });
      li.append(text, button);
      list.append(li);
    }
    const sources = $("relay-sources");
    if (!sources.childElementCount) {
      for (const name of relay.sources) {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = name;
        if (["MobileNotes", "Network", "CrashReporter"].includes(name)) input.checked = true;
        label.append(input, name);
        sources.append(label);
      }
    }
    const files = $("relay-files");
    files.replaceChildren();
    for (const file of relay.files) {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = `/api/relay/download?name=${encodeURIComponent(file.name)}`;
      link.textContent = `${file.name} · ${bytes(file.size)}`;
      li.append(link);
      files.append(li);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadSystem() {
  try {
    const [fw, local] = await Promise.all([api("/api/os/firmwares"), api("/api/os/local")]);
    const body = $("fw-rows");
    body.replaceChildren();
    const pick = $("flash-pick");
    const previous = pick.value;
    pick.replaceChildren();
    for (const item of local.files) {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = `${item.name} · ${bytes(item.size)}`;
      pick.append(option);
    }
    for (const item of fw.firmwares) {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${item.version}</td><td>${item.build}</td><td class="${item.signed ? "signed" : "unsigned"}">${item.signed ? "подписана" : "нет"}</td><td>${bytes(item.size)}</td>`;
      const td = document.createElement("td");
      if (item.signed && item.url) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = "Скачать";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await jsonPost("/api/os/download", { url: item.url });
            pollJobs();
          } catch (error) {
            toast(error.message);
            button.disabled = false;
          }
        });
        td.append(button);
      }
      tr.append(td);
      body.append(tr);
    }
    if (previous) pick.value = previous;
    const localList = $("fw-local");
    localList.replaceChildren();
    if (!local.files.length) {
      const li = document.createElement("li");
      li.textContent = "Скачанных IPSW нет";
      localList.append(li);
    }
    for (const item of local.files) {
      const li = document.createElement("li");
      li.textContent = `${item.name} · ${bytes(item.size)} · ${item.mtime}`;
      localList.append(li);
    }
    const profiles = await api("/api/profiles");
    const host = $("profile-list");
    host.replaceChildren();
    const meta = profiles.ProfileMetadata || {};
    const ids = Object.keys(meta);
    if (!ids.length) {
      const li = document.createElement("li");
      li.textContent = "Профилей нет";
      host.append(li);
    }
    for (const id of ids) {
      const info = meta[id] || {};
      const li = document.createElement("li");
      const text = document.createElement("span");
      text.textContent = info.PayloadDisplayName || info.PayloadIdentifier || id;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Снять";
      button.addEventListener("click", async () => {
        try {
          await jsonPost("/api/profiles/remove", { identifier: id });
          loadSystem();
        } catch (error) {
          toast(error.message);
        }
      });
      li.append(text, button);
      host.append(li);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function pollJobs() {
  try {
    const data = await api("/api/jobs");
    const host = $("jobs");
    const running = (data.jobs || []).filter((job) => job.status === "running" || job.status === "error");
    const recent = (data.jobs || []).slice(0, 3);
    if (!recent.length) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    host.replaceChildren();
    for (const job of recent) {
      const line = document.createElement("div");
      line.className = "jobline";
      const name = document.createElement("strong");
      name.textContent = job.name;
      const msg = document.createElement("span");
      msg.textContent = job.message || job.status;
      const track = document.createElement("div");
      track.className = "track";
      const bar = document.createElement("span");
      bar.style.width = `${job.progress || 0}%`;
      track.append(bar);
      line.append(name, msg, track);
      host.append(line);
    }
    if (running.some((job) => job.status === "running")) {
      setTimeout(pollJobs, 1500);
    }
  } catch (_error) {
    /* статус задач не должен забивать экран */
  }
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => show(button.dataset.view));
});

document.querySelectorAll("[data-power]").forEach((button) => {
  button.addEventListener("click", async () => {
    const action = button.dataset.power;
    const words = { sleep: "усыпить", restart: "перезагрузить", shutdown: "выключить" };
    if (!confirm(`${words[action]} айпад?`)) return;
    try {
      await jsonPost("/api/power", { action });
      toast(action === "sleep" ? "Айпад засыпает" : "Команда ушла, экран погаснет");
    } catch (error) {
      toast(error.message);
    }
  });
});

$("shot").addEventListener("click", async () => {
  try {
    const response = await fetch("/api/screenshot");
    if (!response.ok) {
      const body = await response.json();
      throw new Error(body.error || response.statusText);
    }
    $("wallpaper").src = URL.createObjectURL(await response.blob());
  } catch (error) {
    toast(error.message);
  }
});

$("fs-scope").addEventListener("change", () => {
  state.fs.scope = $("fs-scope").value;
  state.fs.path = "/";
  if (state.fs.scope === "media" || state.fs.scope === "crashes") state.fs.bundle = "";
  loadFiles();
});

$("fs-mkdir").addEventListener("click", async () => {
  const name = prompt("Имя папки");
  if (!name) return;
  const parent = state.fs.path === "/" ? "" : state.fs.path;
  try {
    await jsonPost("/api/fs/mkdir", {
      scope: state.fs.scope,
      path: `${parent}/${name}`.replace("//", "/"),
      bundle: state.fs.bundle,
    });
    loadFiles();
  } catch (error) {
    toast(error.message);
  }
});

$("fs-upload").addEventListener("change", async () => {
  const input = $("fs-upload");
  for (const file of input.files) {
    const body = new FormData();
    body.append("file", file);
    body.append("scope", state.fs.scope);
    body.append("path", state.fs.path);
    body.append("bundle", state.fs.bundle || "");
    try {
      await api("/api/fs/upload", { method: "POST", body });
    } catch (error) {
      toast(error.message);
    }
  }
  input.value = "";
  loadFiles();
});

$("app-kind").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  state.kind = button.dataset.kind;
  for (const item of $("app-kind").querySelectorAll("button")) {
    item.classList.toggle("is-on", item === button);
  }
  loadApps();
});
$("app-q").addEventListener("input", paintApps);

async function uploadIpa(file) {
  const body = new FormData();
  body.append("file", file);
  await api("/api/apps/install", { method: "POST", body });
}

async function takeIpas(files) {
  const list = [...files].filter((file) => file.name.toLowerCase().endsWith(".ipa"));
  if (!list.length) {
    toast("Нужен файл .ipa");
    return;
  }
  try {
    for (const file of list) await uploadIpa(file);
    toast(list.length > 1 ? `В очереди ${list.length} пакета` : `В очереди ${list[0].name}`);
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
}

$("app-ipa").addEventListener("change", async () => {
  const input = $("app-ipa");
  await takeIpas(input.files);
  input.value = "";
});

const ipaDrop = $("ipa-drop");
ipaDrop.addEventListener("dragover", (event) => {
  event.preventDefault();
  ipaDrop.classList.add("over");
});
ipaDrop.addEventListener("dragleave", () => ipaDrop.classList.remove("over"));
ipaDrop.addEventListener("drop", async (event) => {
  event.preventDefault();
  ipaDrop.classList.remove("over");
  await takeIpas(event.dataTransfer.files);
});

api("/api/ipa").then((data) => {
  $("ipa-folder").textContent = `Или положите файл в ${data.folder}`;
}).catch(() => {});

$("log-toggle").addEventListener("click", () => {
  if (state.socket) stopLog(true);
  else startLog();
});
$("log-clear").addEventListener("click", () => {
  state.log = [];
  paintLog();
});
$("log-q").addEventListener("input", paintLog);

async function loadSpeed() {
  const node = $("speed-state");
  node.textContent = "Смотрю устройство…";
  try {
    const data = await api("/api/workshop");
    if (!data.connected) {
      node.textContent = data.error || "Айпад не отвечает";
      return;
    }
    const bits = [
      data.chip ? `чип ${data.chip}` : data.product,
      data.version ? `iOS ${data.version}` : "",
      data.ram || "",
      data.jailbroken ? "джейлбрейк виден" : "джейлбрейка нет",
      data.afc2 ? "диск открыт" : "",
      data.cydia ? "Cydia есть" : "",
      data.ssh ? "SSH открыт" : "",
      data.checkra1n ? "checkra1n уже скачан" : "checkra1n скачается при запуске",
    ];
    node.textContent = bits.filter(Boolean).join(" · ");
  } catch (error) {
    node.textContent = error.message;
  }
}

$("purge").addEventListener("click", async () => {
  try {
    await jsonPost("/api/speed/purge", {});
    toast("Чистка пошла. Это несколько минут, айпад лучше разблокировать.");
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});

$("lighten").addEventListener("click", async () => {
  try {
    const result = await jsonPost("/api/speed/profile", {});
    toast(result.note || "Профиль отправлен");
  } catch (error) {
    toast(error.message);
  }
});

$("heavy").addEventListener("click", async () => {
  const list = $("heavy-list");
  list.textContent = "";
  const li = document.createElement("li");
  li.textContent = "Считаю, айпад может думать минуту…";
  list.append(li);
  try {
    const data = await api("/api/speed/heavy");
    list.replaceChildren();
    for (const app of (data.apps || []).slice(0, 15)) {
      const row = document.createElement("li");
      row.textContent = `${app.name} · ${bytes(app.total_bytes)}`;
      list.append(row);
    }
    if (!(data.apps || []).length) {
      const empty = document.createElement("li");
      empty.textContent = "Своих программ нет";
      list.append(empty);
    }
  } catch (error) {
    list.replaceChildren();
    toast(error.message);
  }
});

$("jb").addEventListener("click", async () => {
  if (!confirm("Запустить checkra1n? Айпад уйдёт в DFU, пока держите кнопки. Данные не стираются.")) return;
  try {
    await jsonPost("/api/jailbreak", {});
    toast("checkra1n запущен. Смотрите полоску внизу и держите кнопки на айпаде.");
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});

$("bak-start").addEventListener("click", async () => {
  try {
    await jsonPost("/api/backup", { password: $("bak-pass").value });
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});
$("relay-go").addEventListener("click", async () => {
  const sources = [...$("relay-sources").querySelectorAll("input:checked")].map((node) => node.value);
  try {
    await jsonPost("/api/relay", { sources });
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});

$("sys-rename").addEventListener("click", async () => {
  try {
    await jsonPost("/api/name", { name: $("sys-name").value });
    toast("Имя записано");
    refreshStatus();
    loadOverview();
  } catch (error) {
    toast(error.message);
  }
});
$("sys-locale-go").addEventListener("click", async () => {
  try {
    await jsonPost("/api/locale", { language: $("sys-lang").value, locale: $("sys-locale").value });
    toast("Язык записан. Перезагрузите айпад, чтобы он применился.");
  } catch (error) {
    toast(error.message);
  }
});
$("sys-wifi").addEventListener("change", async () => {
  try {
    await jsonPost("/api/wifi", { enabled: $("sys-wifi").checked });
  } catch (error) {
    toast(error.message);
  }
});
$("profile-file").addEventListener("change", async () => {
  const input = $("profile-file");
  const file = input.files[0];
  input.value = "";
  if (!file) return;
  const body = new FormData();
  body.append("file", file);
  try {
    const result = await api("/api/profiles/install", { method: "POST", body });
    toast(result.note || "Профиль отправлен");
  } catch (error) {
    toast(error.message);
  }
});
$("rec-enter").addEventListener("click", async () => {
  if (!confirm("Перевести айпад в recovery? Экран станет кабелем и компьютером.")) return;
  try {
    await jsonPost("/api/recovery", { action: "enter" });
    toast("Айпад перезагружается в recovery");
  } catch (error) {
    toast(error.message);
  }
});
$("rec-exit").addEventListener("click", async () => {
  try {
    await jsonPost("/api/recovery", { action: "exit" });
    toast("Выход из recovery");
  } catch (error) {
    toast(error.message);
  }
});
$("flash-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const ipsw = $("flash-pick").value;
  if (!ipsw) {
    toast("Сначала скачайте подписанный IPSW");
    return;
  }
  try {
    await jsonPost("/api/os/restore", {
      ipsw,
      erase: $("flash-erase").checked,
      confirm: $("flash-confirm").value,
    });
    $("flash-confirm").value = "";
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});
$("erase-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await jsonPost("/api/erase", { confirm: $("erase-confirm").value });
    $("erase-confirm").value = "";
    pollJobs();
  } catch (error) {
    toast(error.message);
  }
});

show(new URLSearchParams(location.search).get("view") || "overview");
refreshStatus();
pollJobs();
setInterval(refreshStatus, 8000);
