"use strict";
const $ = (s) => document.querySelector(s);
let toastTimer;

function notify(message) {
    const toast = $("#toast");
    if (!toast) return;
    clearTimeout(toastTimer);
    toast.textContent = message;
    toast.hidden = false;
    toastTimer = setTimeout(() => { toast.hidden = true; }, 5000);
}

function applyCaseFilters() {
    const table = document.getElementById("cases");
    if (!table) return;
    const searchInput = document.getElementById("search");
    const query = (searchInput ? searchInput.value : "").trim().toLowerCase();
    const activeFilter = document.querySelector(".filter.active");
    const filter = activeFilter ? activeFilter.dataset.filter : "all";
    const rows = table.querySelectorAll("tr");
    let visible = 0;
    rows.forEach((row) => {
        const matchesFilter =
            filter === "all" ||
            row.dataset.status === filter ||
            (filter === "deficiency" && row.dataset.deficiency === "1");
        const matchesSearch = !query || (row.dataset.search || "").includes(query);
        const show = matchesFilter && matchesSearch;
        row.hidden = !show;
        if (show) visible++;
    });
    const empty = document.getElementById("empty");
    if (empty) empty.hidden = visible > 0;
    const resultCount = document.getElementById("result-count");
    if (resultCount) resultCount.textContent = `顯示 ${visible} 筆，共 ${rows.length} 筆案件`;
}

// 工作總覽的清單／月曆切換與翻月都不重整頁面；網址用 replaceState 同步，
// 重新整理後仍停在同一個視圖與月份。連結本身保留 href，JS 失敗時退回整頁載入。
function syncOverviewUrl(params) {
    const url = new URL(window.location.href);
    Object.entries(params).forEach(([key, value]) => url.searchParams.set(key, value));
    history.replaceState(null, "", url);
}

function showView(view) {
    const listView = document.getElementById("list-view");
    const calendarView = document.getElementById("calendar-view");
    if (!listView || !calendarView) return;
    listView.hidden = view !== "list";
    calendarView.hidden = view !== "calendar";
    document.querySelectorAll("[data-view]").forEach((a) => {
        const active = a.dataset.view === view;
        a.classList.toggle("active", active);
        if (active) a.setAttribute("aria-current", "page");
        else a.removeAttribute("aria-current");
    });
    syncOverviewUrl({ view });
}

async function loadCalendarMonth(month, fallbackHref) {
    const calendarView = document.getElementById("calendar-view");
    if (!calendarView) return;
    try {
        const resp = await fetch(`/office/overview/calendar?month=${encodeURIComponent(month)}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        calendarView.innerHTML = await resp.text();
        const wrap = calendarView.querySelector("[data-month]");
        if (wrap) syncOverviewUrl({ view: "calendar", month: wrap.dataset.month });
    } catch (err) {
        window.location.href = fallbackHref;
    }
}

async function openCase(caseKey, focusId) {
    const dialog = document.getElementById("case-dialog");
    const detail = document.getElementById("case-detail");
    if (!dialog || !detail) return;
    detail.innerHTML = '<p style="padding:27px;color:var(--muted);">載入中…</p>';
    if (!dialog.open) dialog.showModal();
    try {
        const resp = await fetch(`/office/case/${encodeURIComponent(caseKey)}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        detail.innerHTML = await resp.text();
        const focus = focusId && document.getElementById(focusId);
        if (focus) {
            if (focus.tagName === "DETAILS") focus.open = true;
            focus.scrollIntoView({ block: "start" });
        }
    } catch (err) {
        detail.innerHTML = '<p style="padding:27px;color:#c0392b;">載入案件資料失敗，請關閉後再試一次。</p>';
    }
}

function closeInfoPops(except) {
    document.querySelectorAll("[data-info-toggle]").forEach((btn) => {
        if (btn === except) return;
        const pop = document.getElementById(btn.dataset.infoToggle);
        if (pop) pop.hidden = true;
        btn.setAttribute("aria-expanded", "false");
    });
}

document.addEventListener("click", (event) => {
    const infoBtn = event.target.closest("[data-info-toggle]");
    if (infoBtn) {
        const pop = document.getElementById(infoBtn.dataset.infoToggle);
        if (!pop) return;
        const willOpen = pop.hidden;
        closeInfoPops(infoBtn);
        pop.hidden = !willOpen;
        infoBtn.setAttribute("aria-expanded", String(willOpen));
        return;
    }
    if (!event.target.closest(".info-pop")) closeInfoPops(null);

    const filterBtn = event.target.closest(".filter[data-filter]");
    if (filterBtn) {
        document.querySelectorAll(".filter").forEach((b) => {
            b.classList.toggle("active", b === filterBtn);
            b.setAttribute("aria-pressed", String(b === filterBtn));
        });
        applyCaseFilters();
        return;
    }
    const priorityBtn = event.target.closest("[data-jump-filter]");
    if (priorityBtn) {
        // 月曆視圖下清單是隱藏的，篩了也看不到——先切回清單再套篩選。
        showView("list");
        const target = document.querySelector(`.filter[data-filter="${priorityBtn.dataset.jumpFilter}"]`);
        if (target) {
            target.click();
            document.getElementById("list-view").scrollIntoView({ behavior: "smooth" });
        }
        return;
    }
    const viewLink = event.target.closest("[data-view]");
    if (viewLink) {
        event.preventDefault();
        showView(viewLink.dataset.view);
        return;
    }
    const monthLink = event.target.closest("[data-cal-month]");
    if (monthLink) {
        event.preventDefault();
        loadCalendarMonth(monthLink.dataset.calMonth, monthLink.href);
        return;
    }
    const caseBtn = event.target.closest("[data-case]");
    if (caseBtn) {
        openCase(caseBtn.dataset.case, caseBtn.dataset.focus);
        return;
    }
    const closeBtn = event.target.closest("[data-close]");
    if (closeBtn) {
        const dialog = document.getElementById(closeBtn.dataset.close);
        if (dialog) dialog.close();
        return;
    }
    if (event.target.id === "reset-search") {
        const search = document.getElementById("search");
        if (search) search.value = "";
        const allFilter = document.querySelector('.filter[data-filter="all"]');
        if (allFilter) allFilter.click();
        applyCaseFilters();
    }
});

document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeInfoPops(null);
});

const searchInput = document.getElementById("search");
if (searchInput) searchInput.addEventListener("input", applyCaseFilters);

applyCaseFilters();

// 操作失敗（例如未填處理人）時，伺服器帶 ?case=案件鍵 回來，自動重開該案件，已填的說明會保留
const reopenCase = new URL(window.location.href).searchParams.get("case");
if (reopenCase && /^[A-Za-z0-9]{1,10}-[A-Za-z0-9]{1,20}$/.test(reopenCase)) openCase(reopenCase);

// 網址帶 #錨點 時，自動展開該處（及外層）收合的 <details>；伺服器端也會用 ?open= 預先展開，這裡只是補強。
(function () {
    function openTarget() {
        if (!location.hash) return;
        let el;
        try { el = document.getElementById(decodeURIComponent(location.hash.slice(1))); } catch (e) { return; }
        for (let node = el; node; node = node.parentElement) {
            if (node.tagName === "DETAILS") node.open = true;
        }
        if (el) el.scrollIntoView({ block: "start" });
    }
    window.addEventListener("hashchange", openTarget);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", openTarget); else openTarget();
})();
