(() => {
  "use strict";

  const report = JSON.parse(document.getElementById("reportData").textContent);
  const officialCodes = new Set(report.candidates.map((candidate) => candidate.code));
  const candidates = [...report.candidates, ...(report.extended_watchlist ?? [])];
  $("#candidateCountTitle").textContent = report.extended_watchlist?.length
    ? `📊 적격 후보 ${report.candidates.length} + 참고 ${report.extended_watchlist.length}`
    : `📊 적격 후보 ${report.candidates.length}`;
  const quantBaseline = report.model_id.includes("no-llm");
  const actionable = report.strategy_status === "ACTIVE"
    && report.source_bar_interval_minutes === 1
    && [10, 30, 60].includes(report.analysis_bar_interval_minutes);
  const state = { window: "7" };
  const selectionKey = `danta-watch-draft:v3:${report.data_as_of}`;
  const selection = new Map();
  let copyBusy = false;
  let toastTimer;
  const won = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });
  const one = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 });
  const allocationOne = new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  const flowOne = new Intl.NumberFormat("ko-KR", {
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  });
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const h = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const n = (value) => Number(value);
  const metric = (candidate) => candidate.windows[state.window];
  const ranked = () => [...candidates].sort(
    (a, b) => (metric(a).rank ?? 999) - (metric(b).rank ?? 999),
  );
  const gradeLabel = (grade) => ({
    STRONG_RECOMMEND: "적극 추천", RECOMMEND: "추천",
    NOT_RECOMMEND: "비추천", STRONG_NOT_RECOMMEND: "적극 비추천",
  })[grade] || grade;
  const reviewGradeLabel = (grade) => (
    quantBaseline ? `정량 ${gradeLabel(grade)}` : gradeLabel(grade)
  );
  const gradeClass = (grade) => ({
    STRONG_RECOMMEND: "grade-strong", RECOMMEND: "grade-recommend",
    NOT_RECOMMEND: "grade-avoid", STRONG_NOT_RECOMMEND: "grade-strong-avoid",
  })[grade] || "";
  const recommended = (grade) => grade === "STRONG_RECOMMEND" || grade === "RECOMMEND";
  const signed = (value, suffix = "") => {
    const number = n(value);
    return `${flowOne.format(number)}${suffix}`;
  };
  const percent = (value) => {
    const number = n(value);
    return `${number < 0 ? "-" : ""}${one.format(Math.abs(number))}%`;
  };
  const tone = (value) => n(value) > 0 ? "pos" : n(value) < 0 ? "neg" : "";
  const formatDate = (value) => new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(new Date(value));

  try {
    if (!actionable) throw new Error("research report");
    const saved = JSON.parse(localStorage.getItem(selectionKey) || "{}");
    Object.entries(saved).slice(0, 3).forEach(([code, draft]) => {
      const candidateExists = candidates.some((candidate) => candidate.code === code);
      if (candidateExists && draft && typeof draft === "object") {
        selection.set(code, {
          entryTargetPrice: n(draft.entryTargetPrice) || 0,
          auto: draft.auto !== false,
          allocationPct: cleanAllocation(draft.allocationPct),
        });
      }
    });
  } catch {
    try { localStorage.removeItem(selectionKey); } catch { /* no local storage */ }
  }

  function saveSelection() {
    try { localStorage.setItem(selectionKey, JSON.stringify(Object.fromEntries(selection))); } catch { /* local only */ }
  }

  function cleanPrice(value) {
    return Math.min(10_000_000_000, Math.max(0, n(String(value).replace(/[^\d]/g, "")) || 0));
  }

  function cleanAllocation(value) {
    const raw = String(value ?? "").replace(/[^\d.]/g, "");
    const dot = raw.indexOf(".");
    const normalized = dot < 0
      ? raw
      : `${raw.slice(0, dot + 1)}${raw.slice(dot + 1).replace(/\./g, "").slice(0, 1)}`;
    return Math.min(100, Math.max(0, n(normalized) || 0));
  }

  function rebalanceAllocations() {
    if (!selection.size) return;
    const equalPct = Math.floor((100 / selection.size) * 10) / 10;
    selection.forEach((draft) => { draft.allocationPct = equalPct; });
  }

  function autoEntryTarget(candidate) {
    return Math.max(1, Math.round(n(metric(candidate).box_low)));
  }

  function refreshAutoTargets() {
    selection.forEach((draft, code) => {
      if (!draft.auto) return;
      const candidate = candidates.find((item) => item.code === code);
      if (candidate) draft.entryTargetPrice = autoEntryTarget(candidate);
    });
    saveSelection();
  }

  function filtered() {
    return ranked().filter((candidate) => {
      const item = metric(candidate);
      if (item.structure_status === "WARMING_UP") return true;
      return (!$("#recommendedOnly").checked || recommended(item.ai_grade))
        && (!$("#lowerOnly").checked || n(item.position_pct) <= 35);
    });
  }

  function grade(item) {
    if (item.structure_status === "WARMING_UP") return structureCell(item, "");
    return `<span class="grade ${gradeClass(item.ai_grade)}">${h(reviewGradeLabel(item.ai_grade))}</span>`;
  }

  function stockCell(candidate, extra = "") {
    return `<td class="sticky-name ${extra}"><span class="stock-name">${h(candidate.name)}</span><span class="stock-meta">${h(candidate.code)} · ${h(candidate.sector)}</span></td>`;
  }

  function sparkline(item, name, code) {
    if (item.structure_status === "WARMING_UP" || !item.closes.length) return "";
    const values = item.closes.map(n);
    const width = 132;
    const height = 34;
    const pad = 2;
    const low = Math.min(n(item.box_low), ...values);
    const high = Math.max(n(item.box_high), ...values);
    const spread = Math.max(1, high - low);
    const x = (index) => pad + index * ((width - pad * 2) / Math.max(1, values.length - 1));
    const y = (value) => pad + (high - value) / spread * (height - pad * 2);
    const points = values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
    const upper = y(n(item.box_high));
    const lower = y(n(item.box_low));
    return `<button type="button" class="spark-button" data-chart-code="${h(code)}" aria-label="${h(name)} ${state.window}일 60분봉 상세 보기"><svg class="spark" viewBox="0 0 ${width} ${height}" aria-hidden="true">
      <rect class="spark-band" x="${pad}" y="${upper}" width="${width - pad * 2}" height="${Math.max(1, lower - upper)}"></rect>
      <line class="spark-bound" x1="${pad}" y1="${upper}" x2="${width - pad}" y2="${upper}"></line>
      <line class="spark-bound" x1="${pad}" y1="${lower}" x2="${width - pad}" y2="${lower}"></line>
      <polyline class="spark-line" points="${points}"></polyline>
    </svg></button>`;
  }

  function metricPair(medianValue, maxValue) {
    return `<b>${one.format(n(medianValue))}%</b><small class="target-detail">최대 ${one.format(
      n(maxValue),
    )}%</small>`;
  }

  function targetDaysCell(item) {
    return `<b>${item.reach_days_5pct}/${item.reach_days_10pct}/${item.reach_days_15pct}일</b>`;
  }

  function lowerTrendCell(item) {
    const className = item.lower_trend === "상승" ? "pos" : item.lower_trend === "하락" ? "neg" : "";
    return `<b class="${className}">${h(item.lower_trend)}</b><small class="target-detail">${percent(
      item.lower_trend_pct,
    )}</small>`;
  }

  function positionLabel(value) {
    const position = n(value);
    if (position <= 20) return "하단 핵심권";
    if (position <= 35) return "하단 진입권";
    if (position <= 50) return "중간 하단";
    if (position <= 70) return "중간 상단";
    return "상단권";
  }

  function structureCell(item, content) {
    if (item.structure_status !== "WARMING_UP") return content;
    const completed = Math.max(0, n(item.structure_completed_days));
    return `<span class="structure-warming">분봉 수집 중<br>${completed}/${item.days}거래일</span>`;
  }

  function newsHtml(candidate) {
    if (!candidate.news.length) return "수집된 뉴스 없음";
    return candidate.news.slice(0, 2).map((news) => `
      <a class="news-link" href="${h(news.url)}" target="_blank" rel="noopener noreferrer">${h(news.title)}</a>
      <span class="news-meta">${h(news.source)} · ${formatDate(news.published_at)}</span>`).join("");
  }

  function renderRanking() {
    const rows = filtered();
    $("#visibleCount").textContent = rows.length;
    $("#totalCount").textContent = candidates.length;
    $("#rankingBody").innerHTML = rows.map((candidate) => {
      const item = metric(candidate);
      const flows = item.flows;
      const official = officialCodes.has(candidate.code);
      const selectable = official && actionable && item.structure_status !== "WARMING_UP";
      const selectionLabel = official ? `${candidate.name} 선택` : `${candidate.name} 확장 관찰군`;
      return `<tr class="${official ? "" : "extended-watch-row"}">
        <td class="sticky-select"><input class="candidate-check" data-select-code="${h(candidate.code)}" type="checkbox" aria-label="${h(selectionLabel)}" ${selection.has(candidate.code) ? "checked" : ""} ${selectable ? "" : "disabled"}></td>
        <td class="sticky-rank">${structureCell(item, `${item.rank}${official ? "" : '<small class="extended-badge">관찰</small>'}`)}</td>
        ${stockCell(candidate)}
        <td>${grade(item)}</td>
        <td>${won.format(n(candidate.current_price))}</td>
        <td class="${tone(item.return_pct)}"><b>${percent(item.return_pct)}</b></td>
        <td class="${tone(item.current_vs_window_high_pct)}">${structureCell(item, `<b>${percent(item.current_vs_window_high_pct)}</b>`)}</td>
        <td>${structureCell(item, sparkline(item, candidate.name, candidate.code))}</td>
        <td>${structureCell(item, won.format(n(item.box_high)))}</td>
        <td>${structureCell(item, won.format(n(item.box_low)))}</td>
        <td class="position">${structureCell(item, `${one.format(n(item.position_pct))}%<small class="target-detail">${positionLabel(item.position_pct)}</small><div class="position-track"><span style="width:${Math.max(0, Math.min(100, n(item.position_pct)))}%"></span></div>`)}</td>
        <td>${structureCell(item, lowerTrendCell(item))}</td>
        <td>${structureCell(item, metricPair(item.median_daily_range_pct, item.max_daily_range_pct))}</td>
        <td>${structureCell(item, metricPair(item.median_daily_rebound_pct, item.max_daily_rebound_pct))}</td>
        <td>${structureCell(item, targetDaysCell(item))}</td>
        <td class="${tone(flows.retail)}">${signed(flows.retail)}</td>
        <td class="${tone(flows.foreign)}">${signed(flows.foreign)}</td>
        <td class="${tone(flows.institution)}">${signed(flows.institution)}</td>
        <td class="${tone(flows.financial_investment)}">${signed(flows.financial_investment)}</td>
        <td class="${tone(flows.pension)}">${signed(flows.pension)}</td>
        <td class="${tone(flows.strength_pct)}"><b>${percent(flows.strength_pct)}</b></td>
        <td>${structureCell(item, one.format(n(item.quant_score)))}</td>
        <td>${structureCell(item, one.format(n(item.ai_score)))}</td>
        <td class="wrap">${structureCell(item, h(item.ai_comment))}</td>
        <td class="news-cell">${newsHtml(candidate)}</td>
        <td class="discussion">${h(candidate.discussion_summary)}</td>
        <td><a class="chart-link" href="${h(candidate.naver_url)}" target="_blank" rel="noopener noreferrer">차트보기</a></td>
      </tr>`;
    }).join("");
    bindSelectionInputs();
    bindChartButtons();
  }

  function expandedPlot(item, name, currentPrice) {
    const bars = item.chart_bars;
    const values = bars.map((bar) => n(bar.close));
    const width = 920;
    const height = 220;
    const padLeft = 78;
    const labelGutter = 132;
    const plotRight = width - labelGutter;
    const legendX = plotRight + 8;
    const padTop = 16;
    const padBottom = 30;
    const plotBottom = height - padBottom;
    const low = Math.min(n(item.box_low), ...bars.map((bar) => n(bar.low)));
    const current = n(currentPrice);
    const target10 = current * 1.10;
    const high = Math.max(
      target10,
      ...bars.map((bar) => n(bar.high)),
    );
    const spread = Math.max(1, high - low);
    const x = (index) => padLeft + index * ((plotRight - padLeft) / Math.max(1, values.length - 1));
    const y = (value) => padTop + (high - value) / spread * (plotBottom - padTop);
    const points = values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(" ");
    const lowerY = y(n(item.box_low));
    const currentY = y(current);
    const targetY = y(target10);
    const priceTicks = Array.from({ length: 5 }, (_, index) => high - (spread * index / 4));
    const dateGroups = [];
    bars.forEach((bar, index) => {
      const last = dateGroups.at(-1);
      if (last?.date === bar.trading_date) last.end = index;
      else dateGroups.push({ date: bar.trading_date, start: index, end: index });
    });
    return `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${h(name)} ${state.window}일 60분봉 종가 흐름">
      ${priceTicks.map((value) => {
        const tickY = y(value);
        return `<line class="modal-grid-line" x1="${padLeft}" y1="${tickY}" x2="${plotRight}" y2="${tickY}"></line>
          <text class="modal-price-tick" x="${padLeft - 8}" y="${tickY + 3}" text-anchor="end">${won.format(Math.round(value))}원</text>`;
      }).join("")}
      ${dateGroups.map((group) => {
        const startX = x(group.start);
        const centerX = x((group.start + group.end) / 2);
        const label = `${group.date.slice(4, 6)}.${group.date.slice(6, 8)}`;
        return `<line class="modal-date-line" x1="${startX}" y1="${padTop}" x2="${startX}" y2="${plotBottom}"></line>
          <text class="modal-date-tick" x="${centerX}" y="${height - 8}" text-anchor="middle">${h(label)}</text>`;
      }).join("")}
      <line class="modal-target-line" x1="${padLeft}" y1="${targetY}" x2="${plotRight}" y2="${targetY}"></line>
      <line class="modal-lower-line" x1="${padLeft}" y1="${lowerY}" x2="${plotRight}" y2="${lowerY}"></line>
      <line class="modal-current-line" x1="${padLeft}" y1="${currentY}" x2="${plotRight}" y2="${currentY}"></line>
      <g class="modal-reference-legend" role="group" aria-label="차트 기준선 범례">
        <line class="modal-target-line" x1="${legendX}" y1="26" x2="${legendX + 18}" y2="26"></line>
        <text class="modal-target-label" x="${legendX + 24}" y="29">상단(현재가+10%)</text>
        <line class="modal-lower-line" x1="${legendX}" y1="45" x2="${legendX + 18}" y2="45"></line>
        <text class="modal-lower-label" x="${legendX + 24}" y="48">박스 하단</text>
        <line class="modal-current-line" x1="${legendX}" y1="64" x2="${legendX + 18}" y2="64"></line>
        <text class="modal-current-label" x="${legendX + 24}" y="67">현재가</text>
      </g>
      <polyline class="modal-price-line" points="${points}"></polyline>
    </svg>`;
  }

  function openChartModal(code) {
    const candidate = candidates.find((item) => item.code === code);
    if (!candidate) return;
    const item = metric(candidate);
    if (item.structure_status !== "READY" || !item.chart_bars.length) return;
    $("#chartModalTitle").textContent = `${candidate.name} · 현재가 ${won.format(
      n(candidate.current_price),
    )}원 · ${state.window}일 60분봉`;
    $("#chartModalSummary").textContent = `일중 진폭 ${one.format(n(item.median_daily_range_pct))}% / 최대 ${one.format(
      n(item.max_daily_range_pct),
    )}% · 저점 반등 ${one.format(n(item.median_daily_rebound_pct))}% / 최대 ${one.format(
      n(item.max_daily_rebound_pct),
    )}% · +5/+10/+15% ${item.reach_days_5pct}/${item.reach_days_10pct}/${item.reach_days_15pct}일 · 하단 ${item.lower_trend}`;
    $("#chartModalPlot").innerHTML = expandedPlot(item, candidate.name, candidate.current_price);
    const buckets = [...new Set(item.chart_bars.map((bar) => bar.bucket))].sort();
    const dates = [...new Set(item.chart_bars.map((bar) => bar.trading_date))].sort();
    const byDateBucket = new Map(
      item.chart_bars.map((bar) => [`${bar.trading_date}:${bar.bucket}`, bar]),
    );
    $("#chartModalHead").innerHTML = `<tr><th>거래일</th>${buckets.map(
      (bucket) => `<th>${h(bucket)}시</th>`,
    ).join("")}</tr>`;
    $("#chartModalBody").innerHTML = dates.map((date) => `
      <tr>
        <th>${h(`${date.slice(4, 6)}.${date.slice(6, 8)}`)}</th>
        ${buckets.map((bucket) => {
          const bar = byDateBucket.get(`${date}:${bucket}`);
          if (!bar) return '<td class="empty-bar">-</td>';
          return `<td class="hour-bar-cell">
            <strong>${won.format(n(bar.close))}</strong>
            <small>량 ${won.format(n(bar.volume))} · 시 ${won.format(n(bar.open))}</small>
            <small>저 ${won.format(n(bar.low))} · 고 ${won.format(n(bar.high))}</small>
          </td>`;
        }).join("")}
      </tr>`).join("");
    $("#chartModal").hidden = false;
    document.body.classList.add("modal-open");
    $(".chart-modal-close").focus();
  }

  function closeChartModal() {
    $("#chartModal").hidden = true;
    document.body.classList.remove("modal-open");
  }

  function bindChartButtons() {
    $$(".spark-button").forEach((button) => button.addEventListener(
      "click", () => openChartModal(button.dataset.chartCode),
    ));
  }

  function bindSelectionInputs() {
    $$(".candidate-check").forEach((checkbox) => checkbox.addEventListener("change", () => {
      const code = checkbox.dataset.selectCode;
      if (checkbox.checked && !selection.has(code) && selection.size >= 3) {
        checkbox.checked = false;
        $("#selectionMessage").textContent = "최대 3개까지만 선택할 수 있습니다.";
        return;
      }
      if (checkbox.checked) {
        const candidate = candidates.find((item) => item.code === code);
        if (candidate) {
          selection.set(code, {
            entryTargetPrice: autoEntryTarget(candidate),
            auto: true,
            allocationPct: 0,
          });
        }
      } else selection.delete(code);
      rebalanceAllocations();
      saveSelection();
      renderAll();
    }));
  }

  function updateEntryTarget(code, value) {
    const draft = selection.get(code);
    if (!draft) return;
    draft.entryTargetPrice = cleanPrice(value);
    draft.auto = false;
    saveSelection();
    updateCopyAvailability();
  }

  function updateAllocation(code, value) {
    const draft = selection.get(code);
    if (!draft) return;
    draft.allocationPct = cleanAllocation(value);
    saveSelection();
    updateCopyAvailability();
  }

  function selectedCandidates() {
    return [...selection.keys()]
      .map((code) => candidates.find((candidate) => candidate.code === code))
      .filter(Boolean);
  }

  function updateCopyAvailability() {
    const items = selectedCandidates();
    const allocationTotal = items.reduce((sum, candidate) => {
      const draft = selection.get(candidate.code);
      return sum + (draft ? n(draft.allocationPct) : 0);
    }, 0);
    const invalidEntry = items.some((candidate) => {
      const draft = selection.get(candidate.code);
      return !draft || !draft.entryTargetPrice || n(draft.allocationPct) <= 0
        || metric(candidate).structure_status === "WARMING_UP";
    });
    $("#copySelection").disabled = copyBusy
      || !actionable || items.length === 0 || invalidEntry || allocationTotal > 100;
    const cashPct = Math.max(0, 100 - allocationTotal);
    const summary = $("#allocationSummary");
    summary.textContent = allocationTotal > 100
      ? `배정 합계 ${allocationOne.format(allocationTotal)}% · ${allocationOne.format(allocationTotal - 100)}% 초과`
      : `배정 합계 ${allocationOne.format(allocationTotal)}% · 현금 ${allocationOne.format(cashPct)}%`;
    summary.classList.toggle("invalid", allocationTotal > 100);
  }

  function renderSelectionTray() {
    const items = selectedCandidates();
    $("#selectionCount").textContent = items.length;
    $("#selectionItems").innerHTML = items.map((candidate) => `
      <div class="selection-item">
        <strong>${h(candidate.name)}</strong><small>${h(reviewGradeLabel(metric(candidate).ai_grade))}</small>
        <div class="selection-fields">
          <label><span>진입 목표가(원)</span>
            <input class="price-input tray-price" data-tray-code="${h(candidate.code)}" type="text" inputmode="numeric" aria-label="${h(candidate.name)} 진입 목표가" value="${won.format(selection.get(candidate.code).entryTargetPrice)}">
          </label>
          <label><span>비율(%)</span>
            <input class="ratio-input tray-allocation" data-allocation-code="${h(candidate.code)}" type="text" inputmode="decimal" aria-label="${h(candidate.name)} 주문가능현금 비율" value="${allocationOne.format(selection.get(candidate.code).allocationPct)}">
          </label>
        </div>
      </div>`).join("");
    $$(".tray-price").forEach((input) => {
      input.addEventListener("input", () => updateEntryTarget(input.dataset.trayCode, input.value));
      input.addEventListener("blur", () => {
        const draft = selection.get(input.dataset.trayCode);
        input.value = draft && draft.entryTargetPrice ? won.format(draft.entryTargetPrice) : "";
      });
    });
    $$(".tray-allocation").forEach((input) => {
      input.addEventListener("input", () => updateAllocation(input.dataset.allocationCode, input.value));
      input.addEventListener("blur", () => {
        const draft = selection.get(input.dataset.allocationCode);
        input.value = draft ? allocationOne.format(draft.allocationPct) : "";
      });
    });
    updateCopyAvailability();
  }

  function selectionDraft() {
    const selected = selectedCandidates();
    const totalAllocationPct = selected.reduce(
      (sum, candidate) => sum + n(selection.get(candidate.code).allocationPct), 0,
    );
    const lines = [
      "DANTA ENTRY_MANDATE",
      `report_data_as_of: ${report.data_as_of}`,
      `window_days: ${state.window}`,
      "authority: ENTRY_APPROVAL",
      "execution_mode: USE_LOCKED_ACTIVE_MODE",
      "capital_scope: KIS_ORDERABLE_CASH",
      "allocation_policy: USER_DEFINED_ORDERABLE_CASH_PERCENT",
      `total_allocation_pct: ${allocationOne.format(totalAllocationPct)}`,
      `unallocated_cash_pct: ${allocationOne.format(Math.max(0, 100 - totalAllocationPct))}`,
      `selected_symbol_count: ${selected.length}`,
      "entry_trigger: LAST_PRICE_LTE_TARGET",
      "validity_policy: UNTIL_FILLED_OR_BOX_INVALIDATED",
      "partial_fill_policy: PROTECT_FILLED_CANCEL_REMAINDER_ON_INVALIDATION",
      "duplicate_guard: INTERNAL_ON_INGEST",
      "hard_stop_pct: -7.0",
      "profit_policy: ACTIVE_VERSIONED_LOCAL_ENGINE",
      "selections:",
    ];
    selected.forEach((candidate) => {
      const item = metric(candidate);
      const draft = selection.get(candidate.code);
      lines.push(
        `- rank: ${item.rank}`, `  symbol: ${candidate.code}`, `  name: ${candidate.name}`,
        `  entry_target_price_krw: ${draft.entryTargetPrice}`,
        `  entry_price_source: ${draft.auto ? "BOX_LOW_AUTO" : "USER_EDITED"}`,
        `  allocation_pct: ${allocationOne.format(draft.allocationPct)}`,
        `  ai_grade: ${reviewGradeLabel(item.ai_grade)}`,
        `  box_low: ${item.box_low}`, `  box_high: ${item.box_high}`,
      );
    });
    lines.push("request: 승인문을 검증하고 현재 잠긴 계좌 모드에서 목표가 도달 시 자동매수를 위임한다.");
    return lines.join("\n");
  }

  function showCopyToast(message, isError = false) {
    const toast = $("#copyToast");
    window.clearTimeout(toastTimer);
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.hidden = false;
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 2600);
  }

  function renderAll() {
    renderRanking();
    renderSelectionTray();
  }

  $$(".window-picker button").forEach((button) => button.addEventListener("click", () => {
    state.window = button.dataset.window;
    $$(".window-picker button").forEach((item) => item.classList.toggle("active", item === button));
    refreshAutoTargets();
    renderAll();
  }));
  ["#recommendedOnly", "#lowerOnly"].forEach((selector) => {
    $(selector).addEventListener("change", renderAll);
  });
  $("#toggleSelection").addEventListener("click", () => {
    if (!actionable) {
      showCopyToast("연구용 보고서는 자동매수 승인에 사용할 수 없습니다", true);
      return;
    }
    const willShow = $("#selectionTray").hidden;
    $("#selectionTray").hidden = !willShow;
    const label = willShow ? "선택창 숨김" : "선택창 표시";
    $("#toggleSelection").setAttribute("aria-label", label);
    $("#toggleSelection").setAttribute("title", label);
    $("#toggleSelection").classList.toggle("active", willShow);
    $("#toggleSelection").setAttribute("aria-expanded", String(willShow));
  });
  $("#clearSelection").addEventListener("click", () => {
    selection.clear();
    saveSelection();
    $("#selectionMessage").textContent = "선택 초안을 모두 지웠습니다.";
    renderAll();
  });
  $$("[data-close-chart]").forEach((button) => button.addEventListener("click", closeChartModal));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#chartModal").hidden) closeChartModal();
  });
  const copyButton = $("#copySelection");
  const copyButtonIdleHtml = copyButton.innerHTML;
  copyButton.addEventListener("click", async () => {
    if (copyButton.disabled || copyBusy) return;
    copyBusy = true;
    copyButton.innerHTML = '<span class="button-spinner" aria-hidden="true"></span>';
    copyButton.setAttribute("aria-label", "승인문 복사 중");
    copyButton.setAttribute("title", "승인문 복사 중");
    updateCopyAvailability();
    try {
      const copyResult = navigator.clipboard.writeText(selectionDraft())
        .then(() => true, () => false);
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      if (!await copyResult) throw new Error("clipboard write failed");
      copyButton.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24"><path d="m9.2 16.2-4.4-4.4-1.6 1.6 6 6L21 7.6 19.4 6 9.2 16.2Z"/></svg>';
      copyButton.setAttribute("aria-label", "승인문 복사 완료");
      copyButton.setAttribute("title", "승인문 복사 완료");
      $("#selectionMessage").textContent = "승인문 복사 완료 — Android Codex 앱에 붙여넣으면 자동매수가 위임됩니다.";
      showCopyToast("승인문 복사 완료 · Codex에 붙여넣으세요");
      await new Promise((resolve) => window.setTimeout(resolve, 700));
    } catch {
      $("#selectionMessage").textContent = "복사 권한이 없습니다. 브라우저 권한을 확인해주세요.";
      showCopyToast("복사하지 못했습니다 · 브라우저 권한을 확인하세요", true);
    } finally {
      copyBusy = false;
      copyButton.innerHTML = copyButtonIdleHtml;
      copyButton.setAttribute("aria-label", "자동매수 승인문 복사");
      copyButton.setAttribute("title", "자동매수 승인문 복사");
      updateCopyAvailability();
    }
  });

  $("#marketRegime").textContent = report.market_regime;
  $("#dataAsOf").textContent = `기준 ${formatDate(report.data_as_of)}`;
  $("#demoBadge").hidden = !report.is_demo;
  const strategyBadge = $("#strategyBadge");
  strategyBadge.textContent = actionable
    ? `${report.analysis_bar_interval_minutes}분봉 운영 후보`
    : "연구용 · 주문 불가";
  strategyBadge.classList.toggle("active", actionable);
  $("#versions").textContent = `${report.calculation_version} · ${report.model_id} · ${report.prompt_version}`;
  if (!actionable) {
    $("#toggleSelection").setAttribute("aria-disabled", "true");
    $("#toggleSelection").setAttribute("title", "60분봉 운영 후보 전환 전에는 선택할 수 없습니다");
    $("#analysisDescription").textContent = report.source_bar_interval_minutes === 1
      && report.analysis_bar_interval_minutes
      ? `실제 1분봉을 ${report.analysis_bar_interval_minutes}분봉으로 집계한 연구 기준선입니다. 뉴스·공시 AI 전수 검토와 주문 연결은 아직 비활성화되어 있습니다.`
      : "기존 일봉 연구 기준선이며 주문에 사용할 수 없습니다.";
  }
  if (quantBaseline) {
    if (actionable) {
      $("#analysisDescription").textContent = "박스·수익률·차트·수급·정량 기준선을 한 행에서 비교합니다. AI 정성 검토는 아직 미연결입니다.";
    }
    $("#recommendFilterLabel").textContent = "정량 추천 이상";
    $("#reviewGradeHeader").textContent = "정량 등급";
    $("#reviewScoreHeader").textContent = "검토점수";
    $("#reviewCommentHeader").textContent = "정량 코멘트";
  }
  renderAll();
})();
