(() => {
  "use strict";

  const data = window.BAY_OUTLOOK_DATA;
  if (!data || !data.meta) {
    document.body.innerHTML = '<main class="empty-state"><div><strong>Dashboard data unavailable</strong>The Phase 8 payload could not be loaded.</div></main>';
    return;
  }

  const validViews = new Set(["overview", "county", "education", "health"]);
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const metricById = new Map(data.metrics.map((metric) => [metric.metricId, metric]));
  const countyByCode = new Map(data.counties.map((county) => [county.countyCode, county]));
  const sourceById = new Map(data.sources.map((source) => [source.sourceId, source]));
  const state = {
    view: validViews.has(location.hash.slice(1)) ? location.hash.slice(1) : "overview",
    comparisonMetric: "unemployment_rate",
    trendMetric: "unemployment_rate",
    countyCode: data.counties[0] ? data.counties[0].countyCode : "",
  };

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const titleCase = (value) => String(value ?? "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());

  const formatDate = (value) => {
    if (!value) return "Not available";
    const date = new Date(`${String(value).slice(0, 10)}T12:00:00Z`);
    if (Number.isNaN(date.valueOf())) return String(value);
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" }).format(date);
  };

  const formatPeriod = (value) => {
    const text = String(value ?? "");
    const month = text.match(/^(\d{4})-(0[1-9]|1[0-2])$/);
    if (month) {
      return new Intl.DateTimeFormat("en-US", { month: "long", year: "numeric", timeZone: "UTC" })
        .format(new Date(`${month[1]}-${month[2]}-15T12:00:00Z`));
    }
    const quarter = text.match(/^(\d{4})-Q([1-4])$/i);
    if (quarter) return `Q${quarter[2]} ${quarter[1]}`;
    return text || "Not available";
  };

  const number = (value, digits = 1) => value == null || Number.isNaN(Number(value))
    ? "—"
    : new Intl.NumberFormat("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));

  const compact = (value, digits = 1) => value == null || Number.isNaN(Number(value))
    ? "—"
    : new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: digits }).format(Number(value));

  const signed = (value, digits = 1) => {
    if (value == null || Number.isNaN(Number(value))) return "—";
    const numeric = Number(value);
    const prefix = numeric > 0 ? "+" : numeric < 0 ? "−" : "";
    return `${prefix}${number(Math.abs(numeric), digits)}`;
  };

  const formatValue = (value, unit, precision = 1) => {
    if (value == null) return "—";
    const normalized = String(unit ?? "").toLowerCase();
    if (normalized === "percent") return `${number(value, precision)}%`;
    if (normalized.includes("current dollars")) {
      return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: precision }).format(value);
    }
    if (normalized.includes("thousands of chained")) return `$${compact(Number(value) * 1000, 1)}`;
    if (["persons", "jobs", "establishments"].includes(normalized)) return compact(value, 1);
    return number(value, precision);
  };

  const formatComparison = (row) => {
    const unit = String(row.comparisonUnit ?? "").toLowerCase();
    const precision = Number(row.displayPrecision ?? 1);
    if (unit === "percentage points") return `${signed(row.comparisonValue, precision)} pp`;
    if (unit === "percent") return `${signed(row.comparisonValue, precision)}%`;
    return formatValue(row.comparisonValue, row.comparisonUnit, precision);
  };

  const formatYearChange = (row) => {
    if (!row) return "Insufficient history";
    if (row.changeMethod === "percentage_point") {
      return row.yearChangeAbsolute == null ? "Insufficient annual history" : `${signed(row.yearChangeAbsolute, row.displayPrecision ?? 1)} pp year over year`;
    }
    return row.yearChangePercent == null ? "Insufficient annual history" : `${signed(row.yearChangePercent, 1)}% year over year`;
  };

  const formatAxis = (value, unit) => {
    const normalized = String(unit ?? "").toLowerCase();
    if (normalized === "percent") return `${number(value, Math.abs(value) < 10 ? 1 : 0)}%`;
    if (normalized === "percentage points") return `${number(value, 1)} pp`;
    if (normalized.includes("dollars")) return `$${compact(value, 1)}`;
    if (normalized.includes("thousands of chained")) return `$${compact(value * 1000, 1)}`;
    if (["persons", "jobs", "establishments"].includes(normalized)) return compact(value, 1);
    return compact(value, 1);
  };

  const freshnessLabel = (status) => ({ current: "Current", delayed: "Delayed", stale: "Stale", unknown: "Unknown" }[status] || titleCase(status));
  const readinessLabel = (status) => ({ active: "Active", model_ready: "Model-ready", planned: "Planned" }[status] || titleCase(status));
  const basisLabel = (basis) => ({
    resident: "Resident-based",
    establishment_location: "Establishment location",
    production_location: "Production location",
    household_residence: "Household residence",
    high_school_location: "High-school location",
  }[basis] || titleCase(basis));

  const freshnessBadge = (status) => `<span class="freshness-badge ${status === "stale" ? "badge-stale" : status === "delayed" ? "badge-delayed" : ""}">${escapeHtml(freshnessLabel(status))}</span>`;
  const readinessBadge = (status) => `<span class="state-badge state-${escapeHtml(status)}">${escapeHtml(readinessLabel(status))}</span>`;

  const setText = (selector, value) => {
    const element = $(selector);
    if (element) element.textContent = value;
  };

  const setFreshness = (selector, status) => {
    const element = $(selector);
    if (!element) return;
    element.textContent = freshnessLabel(status);
    element.classList.toggle("badge-stale", status === "stale");
    element.classList.toggle("badge-delayed", status === "delayed");
  };

  const metricLatest = (metricId) => data.latest.filter((row) => row.metricId === metricId);
  const countyLatest = (countyCode) => data.latest.filter((row) => row.countyCode === countyCode);
  const metricSeries = (countyCode, metricId) => data.series
    .filter((row) => row.countyCode === countyCode && row.metricId === metricId && row.value != null)
    .sort((a, b) => a.periodSortKey - b.periodSortKey);

  const svgNode = (tag, attributes = {}, text = null) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text != null) node.textContent = text;
    return node;
  };

  const showTooltip = (event, content) => {
    const tooltip = $("#chart-tooltip");
    tooltip.innerHTML = content;
    tooltip.hidden = false;
    const x = Math.min(event.clientX + 14, window.innerWidth - 245);
    const y = Math.min(event.clientY + 14, window.innerHeight - 110);
    tooltip.style.left = `${Math.max(8, x)}px`;
    tooltip.style.top = `${Math.max(8, y)}px`;
  };

  const hideTooltip = () => {
    $("#chart-tooltip").hidden = true;
  };

  function setView(view, options = {}) {
    if (!validViews.has(view)) return;
    state.view = view;
    $$('[data-view-panel]').forEach((panel) => {
      const active = panel.dataset.viewPanel === view;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    $$(".nav-tab").forEach((tab) => {
      const active = tab.dataset.view === view;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    if (options.updateHash !== false) {
      try {
        history.replaceState(null, "", `#${view}`);
      } catch (_error) {
        location.hash = view;
      }
    }
    if (options.scroll !== false) window.scrollTo({ top: 0, behavior: "smooth" });
    if (view === "county") renderCounty();
    if (view === "education") renderEducation();
    if (view === "health") renderHealth();
  }

  function wireNavigation() {
    $$('[data-view]').forEach((control) => control.addEventListener("click", () => setView(control.dataset.view)));
    $$('[data-view-link]').forEach((link) => link.addEventListener("click", (event) => {
      event.preventDefault();
      setView(link.dataset.viewLink);
    }));
    $$('[data-scroll-to]').forEach((control) => control.addEventListener("click", () => {
      $(`#${control.dataset.scrollTo}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    }));
    $("[data-scroll-top]")?.addEventListener("click", () => window.scrollTo({ top: 0, behavior: "smooth" }));
    window.addEventListener("hashchange", () => {
      const view = location.hash.slice(1);
      if (validViews.has(view)) setView(view, { updateHash: false });
    });
    $$(".nav-tab").forEach((tab, index, tabs) => tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      let next = index;
      if (event.key === "ArrowLeft") next = (index - 1 + tabs.length) % tabs.length;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      tabs[next].focus();
      setView(tabs[next].dataset.view);
    }));
  }

  function renderOverview() {
    const overview = data.overview;
    setText("#header-as-of", formatDate(data.meta.asOfDate));
    $("#fixture-alert").hidden = !data.meta.allowFixtures;
    setText("#pulse-summary", `${overview.recencyCounts.current || 0} current and ${overview.recencyCounts.stale || 0} stale latest series—coverage remains part of the finding.`);
    setText("#pulse-active", overview.activeIndicatorCount);
    setText("#pulse-series", overview.latestSeriesCount);
    setText("#pulse-quality", data.qualityChecks.filter((check) => check.passed).length);

    const unemployment = overview.unemployment;
    setText("#unemployment-value", unemployment.medianLevel == null ? "—" : `${number(unemployment.medianLevel, 1)}%`);
    setText("#unemployment-change", unemployment.medianYearChange == null ? "Insufficient annual history" : `${signed(unemployment.medianYearChange, 1)} pp median change · ${unemployment.improvingCount}/${unemployment.countyCount} improved`);
    setText("#unemployment-period", formatPeriod(unemployment.period));
    setFreshness("#unemployment-freshness", unemployment.recencyStatus);

    const gdp = overview.gdp;
    setText("#gdp-value", gdp.medianGrowth == null ? "—" : `${signed(gdp.medianGrowth, 2)}%`);
    setText("#gdp-change", `${gdp.positiveCount}/${gdp.countyCount} counties posted positive real growth`);
    setText("#gdp-period", formatPeriod(gdp.period));
    setFreshness("#gdp-freshness", gdp.recencyStatus);

    const employment = overview.coveredEmployment;
    setText("#employment-value", `${employment.historyReadyCount}/${employment.countyCount}`);
    setText("#employment-change", employment.historyReadyCount === 0 ? "Annual growth withheld until prior-year history is loaded" : "Annual comparisons available");
    $("#employment-change")?.classList.toggle("is-warning", employment.historyReadyCount === 0);
    setText("#employment-period", formatPeriod(employment.period));
    setFreshness("#employment-freshness", employment.recencyStatus);

    const education = overview.education;
    setText("#education-value", `${education.modelReadyCount}/${education.indicatorCount}`);
    setText("#education-change", education.observationCount === 0 ? "No live education facts yet—no outcome is inferred" : `${compact(education.observationCount, 1)} live observations`);
  }

  function comparisonOptions() {
    const counts = new Map();
    data.latest.forEach((row) => {
      if (row.comparisonValue != null) counts.set(row.metricId, (counts.get(row.metricId) || 0) + 1);
    });
    const priority = ["unemployment_rate", "real_gdp", "average_weekly_wage", "employed_people", "unemployed_people", "college_going_rate_12mo", "ag_ready_share"];
    return data.metrics
      .filter((metric) => (counts.get(metric.metricId) || 0) >= 2 && metric.comparisonBasis !== "none")
      .sort((a, b) => {
        const ai = priority.indexOf(a.metricId);
        const bi = priority.indexOf(b.metricId);
        if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
        return a.metricLabel.localeCompare(b.metricLabel);
      });
  }

  function populateSelectors() {
    const comparison = comparisonOptions();
    const comparisonSelect = $("#comparison-metric");
    comparisonSelect.innerHTML = comparison.map((metric) => `<option value="${escapeHtml(metric.metricId)}">${escapeHtml(metric.metricLabel)}${metric.comparisonBasis === "year_change" ? " · growth" : ""}</option>`).join("");
    if (!comparison.some((metric) => metric.metricId === state.comparisonMetric)) state.comparisonMetric = comparison[0]?.metricId || "";
    comparisonSelect.value = state.comparisonMetric;
    comparisonSelect.addEventListener("change", () => {
      state.comparisonMetric = comparisonSelect.value;
      renderComparison();
    });

    const countySelect = $("#county-select");
    countySelect.innerHTML = data.counties.map((county) => `<option value="${escapeHtml(county.countyCode)}">${escapeHtml(county.countyName)} County</option>`).join("");
    countySelect.value = state.countyCode;
    countySelect.addEventListener("change", () => {
      state.countyCode = countySelect.value;
      renderCounty();
    });

    const seriesCounts = new Map();
    data.series.forEach((row) => seriesCounts.set(row.metricId, (seriesCounts.get(row.metricId) || 0) + 1));
    const trendPriority = ["unemployment_rate", "real_gdp", "employed_people", "unemployed_people", "labor_force", "average_weekly_wage", "average_monthly_covered_employment", "covered_establishments"];
    const trendMetrics = data.metrics
      .filter((metric) => (seriesCounts.get(metric.metricId) || 0) > 0)
      .sort((a, b) => {
        const ai = trendPriority.indexOf(a.metricId);
        const bi = trendPriority.indexOf(b.metricId);
        if (ai !== -1 || bi !== -1) return (ai === -1 ? 999 : ai) - (bi === -1 ? 999 : bi);
        return a.metricLabel.localeCompare(b.metricLabel);
      });
    const trendSelect = $("#trend-metric");
    trendSelect.innerHTML = trendMetrics.map((metric) => `<option value="${escapeHtml(metric.metricId)}">${escapeHtml(metric.metricLabel)}</option>`).join("");
    if (!trendMetrics.some((metric) => metric.metricId === state.trendMetric)) state.trendMetric = trendMetrics[0]?.metricId || "";
    trendSelect.value = state.trendMetric;
    trendSelect.addEventListener("change", () => {
      state.trendMetric = trendSelect.value;
      renderTrend();
    });

    $$('[data-compare-metric]').forEach((button) => button.addEventListener("click", () => {
      const requested = button.dataset.compareMetric;
      if (comparison.some((metric) => metric.metricId === requested)) {
        state.comparisonMetric = requested;
        comparisonSelect.value = requested;
        renderComparison();
        $("#comparison-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }));
  }

  function renderComparison() {
    const metric = metricById.get(state.comparisonMetric);
    const rows = metricLatest(state.comparisonMetric)
      .filter((row) => row.comparisonValue != null)
      .sort((a, b) => (a.countyRank ?? 999) - (b.countyRank ?? 999) || a.countyName.localeCompare(b.countyName));
    const chart = $("#comparison-chart");
    if (!metric || !rows.length) {
      chart.innerHTML = '<div class="empty-state"><div><strong>No comparable county values</strong>This metric needs aligned observations and a reviewed comparison rule.</div></div>';
      setText("#comparison-title", metric?.metricLabel || "County comparison");
      setText("#comparison-subtitle", "Comparison unavailable");
      $("#comparison-table-body").innerHTML = "";
      return;
    }

    const period = rows[0].period;
    const basis = metric.comparisonBasis === "year_change" ? "year-over-year change" : "latest level";
    setText("#comparison-title", `${metric.metricLabel} · ${basis}`);
    setText("#comparison-subtitle", `${formatPeriod(period)} · ${basisLabel(rows[0].geographyBasis)} · ${rows.length} of 9 counties`);

    const width = 920;
    const margin = { top: 24, right: 105, bottom: 48, left: 150 };
    const rowHeight = 42;
    const height = margin.top + rows.length * rowHeight + margin.bottom;
    const values = rows.map((row) => Number(row.comparisonValue));
    let minimum = Math.min(0, ...values);
    let maximum = Math.max(0, ...values);
    if (minimum === maximum) { minimum -= 1; maximum += 1; }
    const padding = (maximum - minimum) * 0.08;
    if (minimum < 0) minimum -= padding;
    if (maximum > 0) maximum += padding;
    const plotWidth = width - margin.left - margin.right;
    const x = (value) => margin.left + ((value - minimum) / (maximum - minimum)) * plotWidth;
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-hidden": "true" });

    for (let index = 0; index <= 4; index += 1) {
      const tick = minimum + ((maximum - minimum) * index) / 4;
      const tickX = x(tick);
      svg.append(svgNode("line", { x1: tickX, y1: margin.top - 5, x2: tickX, y2: height - margin.bottom + 6, class: "grid-line" }));
      svg.append(svgNode("text", { x: tickX, y: height - 16, "text-anchor": "middle", class: "axis-label" }, formatAxis(tick, rows[0].comparisonUnit)));
    }
    const zeroX = x(0);
    svg.append(svgNode("line", { x1: zeroX, y1: margin.top - 7, x2: zeroX, y2: height - margin.bottom + 6, class: "zero-line-svg" }));
    if (rows[0].benchmarkValue != null) {
      const medianX = x(Number(rows[0].benchmarkValue));
      svg.append(svgNode("line", { x1: medianX, y1: margin.top - 10, x2: medianX, y2: height - margin.bottom + 6, class: "median-line-svg" }));
    }

    rows.forEach((row, index) => {
      const y = margin.top + index * rowHeight + 9;
      const valueX = x(Number(row.comparisonValue));
      const barX = Math.min(zeroX, valueX);
      const barWidth = Math.max(2, Math.abs(valueX - zeroX));
      svg.append(svgNode("text", { x: margin.left - 13, y: y + 14, "text-anchor": "end", class: "bar-label" }, row.countyName));
      const rect = svgNode("rect", {
        x: barX,
        y,
        width: barWidth,
        height: 22,
        class: "comparison-bar",
        tabindex: "0",
        role: "img",
        "aria-label": `${row.countyName}: ${formatComparison(row)}, rank ${row.countyRank ?? "not assigned"}`,
      });
      const tooltip = `<strong>${escapeHtml(row.countyName)}</strong><br>${escapeHtml(formatComparison(row))}<br>Median: ${escapeHtml(formatAxis(row.benchmarkValue, row.comparisonUnit))}<br>${escapeHtml(freshnessLabel(row.recencyStatus))}`;
      rect.addEventListener("mousemove", (event) => showTooltip(event, tooltip));
      rect.addEventListener("mouseleave", hideTooltip);
      rect.addEventListener("focus", (event) => showTooltip({ clientX: event.target.getBoundingClientRect().right, clientY: event.target.getBoundingClientRect().top }, tooltip));
      rect.addEventListener("blur", hideTooltip);
      svg.append(rect);
      svg.append(svgNode("text", { x: width - margin.right + 10, y: y + 14, class: "bar-value" }, formatComparison(row)));
    });
    chart.replaceChildren(svg);

    const best = rows.find((row) => row.countyRank === 1) || rows[0];
    const direction = metric.rankDirection === "lower_is_better" ? "Lower values receive the stronger descriptive rank." : "Higher values receive the stronger descriptive rank.";
    const stale = rows.some((row) => row.recencyStatus === "stale") ? " These observations are stale and should not be presented as current conditions." : "";
    $("#comparison-callout").innerHTML = `<strong>${escapeHtml(best.countyName)}</strong> ranks first at ${escapeHtml(formatComparison(best))}. The unweighted available-county median is ${escapeHtml(formatAxis(best.benchmarkValue, best.comparisonUnit))}. ${escapeHtml(direction)}${escapeHtml(stale)}`;
    setText("#comparison-description", `${metric.metricLabel} county comparison for ${formatPeriod(period)}. ${best.countyName} ranks first. The median is ${formatAxis(best.benchmarkValue, best.comparisonUnit)}.`);
    $("#comparison-table-body").innerHTML = rows.map((row) => `<tr><th scope="row">${escapeHtml(row.countyName)}</th><td>${escapeHtml(formatComparison(row))}</td><td>${row.countyRank == null ? "—" : escapeHtml(row.countyRank)}</td><td>${freshnessBadge(row.recencyStatus)}</td></tr>`).join("");
  }

  function profileDefinition(row) {
    if (!row) return { label: "Not available", value: "—", change: "No live observation" };
    if (row.metricId === "real_gdp") {
      return { label: "Real GDP growth", value: row.yearChangePercent == null ? "—" : `${signed(row.yearChangePercent, 2)}%`, change: "Year-over-year real growth" };
    }
    return { label: row.metricLabel, value: formatValue(row.value, row.unit, row.displayPrecision ?? 1), change: formatYearChange(row) };
  }

  function renderCountyProfiles(rows) {
    const definitions = [
      { domain: "Resident labor market", metricId: "unemployment_rate" },
      { domain: "Production economy", metricId: "real_gdp" },
      { domain: "Establishment wages", metricId: "average_weekly_wage" },
    ];
    $("#county-profile-grid").innerHTML = definitions.map((definition) => {
      const row = rows.find((candidate) => candidate.metricId === definition.metricId);
      const profile = profileDefinition(row);
      if (!row) return `<article class="profile-card"><div class="profile-domain"><span>${escapeHtml(definition.domain)}</span><span class="freshness-badge badge-delayed">Unavailable</span></div><h3>${escapeHtml(profile.label)}</h3><p class="profile-value">—</p><p class="profile-change">No validated observation</p></article>`;
      return `<article class="profile-card">
        <div class="profile-domain"><span>${escapeHtml(definition.domain)}</span>${freshnessBadge(row.recencyStatus)}</div>
        <h3>${escapeHtml(profile.label)}</h3>
        <p class="profile-value">${escapeHtml(profile.value)}</p>
        <p class="profile-change">${escapeHtml(profile.change)}</p>
        <div class="profile-meta"><span>Period<strong>${escapeHtml(formatPeriod(row.period))}</strong></span><span>County rank<strong>${row.countyRank == null ? "Not assigned" : `${escapeHtml(row.countyRank)} of ${escapeHtml(row.benchmarkCountyCount)}`}</strong></span></div>
      </article>`;
    }).join("");
  }

  function renderCountyEvidence(rows) {
    const sourceRows = [];
    const seen = new Set();
    ["BLS_LAUS", "BLS_QCEW", "BEA_CAGDP1"].forEach((sourceId) => {
      const candidates = rows.filter((row) => row.sourceId === sourceId);
      if (!candidates.length || seen.has(sourceId)) return;
      seen.add(sourceId);
      sourceRows.push(candidates.sort((a, b) => b.periodSortKey - a.periodSortKey)[0]);
    });
    $("#county-evidence").innerHTML = sourceRows.map((row) => {
      const source = sourceById.get(row.sourceId);
      return `<article class="evidence-card">
        <p class="eyebrow">${escapeHtml(row.sourceId)}</p>
        <h3>${escapeHtml(source?.sourceLabel || row.sourceId)}</h3>
        <p>${escapeHtml(row.interpretationNote || row.notes || "Source-specific interpretation applies.")}</p>
        <dl class="evidence-list">
          <div><dt>Geography</dt><dd>${escapeHtml(basisLabel(row.geographyBasis))}</dd></div>
          <div><dt>Period</dt><dd>${escapeHtml(formatPeriod(row.period))}</dd></div>
          <div><dt>Publication date</dt><dd>${escapeHtml(formatDate(row.publicationDate))}</dd></div>
          <div><dt>Vintage</dt><dd>${escapeHtml(row.sourceVintage)}</dd></div>
          <div><dt>Adjustment</dt><dd>${escapeHtml(titleCase(row.adjustment))}</dd></div>
          <div><dt>Retrieved</dt><dd>${escapeHtml(formatDate(row.retrievedAt))}</dd></div>
          <div><dt>Release status</dt><dd>${escapeHtml(titleCase(row.releaseStatus))}</dd></div>
          <div><dt>Next expected update</dt><dd>${escapeHtml(formatDate(row.nextExpectedUpdate))}</dd></div>
        </dl>
      </article>`;
    }).join("");
  }

  function renderCounty() {
    const county = countyByCode.get(state.countyCode) || data.counties[0];
    if (!county) return;
    const rows = countyLatest(county.countyCode);
    setText("#county-index", String(data.counties.findIndex((item) => item.countyCode === county.countyCode) + 1).padStart(2, "0"));
    setText("#county-name", `${county.countyName} County`);
    $("#county-select").value = county.countyCode;
    renderCountyProfiles(rows);
    renderTrend();
    renderCountyEvidence(rows);
  }

  function renderTrend() {
    const county = countyByCode.get(state.countyCode);
    const metric = metricById.get(state.trendMetric);
    const rows = metricSeries(state.countyCode, state.trendMetric);
    const chart = $("#trend-chart");
    setText("#trend-title", `${county?.countyName || "County"} · ${metric?.metricLabel || "Measure"}`);
    if (!metric || rows.length < 2) {
      const latest = rows[rows.length - 1];
      setText("#trend-subtitle", latest ? `${formatPeriod(latest.period)} · ${basisLabel(latest.geographyBasis)}` : "No observations loaded");
      chart.innerHTML = '<div class="empty-state"><div><strong>History is not yet sufficient</strong>A line is withheld until at least two validated periods are available.</div></div>';
      $("#trend-callout").innerHTML = latest ? `<strong>One period is available:</strong> ${escapeHtml(formatPeriod(latest.period))}, ${escapeHtml(formatValue(latest.value, latest.unit, metric.displayPrecision))}. No trend is inferred.` : "No validated series is available for this county and metric.";
      $("#trend-table-body").innerHTML = latest ? `<tr><th scope="row">${escapeHtml(formatPeriod(latest.period))}</th><td>${escapeHtml(formatValue(latest.value, latest.unit, metric.displayPrecision))}</td><td>—</td><td>${escapeHtml(titleCase(latest.valueStatus))}</td></tr>` : "";
      setText("#trend-description", "Insufficient history for a time-series chart.");
      return;
    }

    const latest = rows[rows.length - 1];
    setText("#trend-subtitle", `${formatPeriod(rows[0].period)}–${formatPeriod(latest.period)} · ${basisLabel(latest.geographyBasis)} · ${titleCase(latest.adjustment)}`);
    const width = 920;
    const height = 350;
    const margin = { top: 24, right: 30, bottom: 52, left: 78 };
    const values = rows.map((row) => Number(row.value));
    let minimum = Math.min(...values);
    let maximum = Math.max(...values);
    if (minimum === maximum) { minimum -= Math.abs(minimum || 1) * 0.05; maximum += Math.abs(maximum || 1) * 0.05; }
    const padding = (maximum - minimum) * 0.1;
    minimum -= padding;
    maximum += padding;
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const x = (index) => margin.left + (index / (rows.length - 1)) * plotWidth;
    const y = (value) => margin.top + ((maximum - value) / (maximum - minimum)) * plotHeight;
    const svg = svgNode("svg", { viewBox: `0 0 ${width} ${height}`, role: "img", "aria-hidden": "true" });

    for (let index = 0; index <= 4; index += 1) {
      const tick = minimum + ((maximum - minimum) * index) / 4;
      const tickY = y(tick);
      svg.append(svgNode("line", { x1: margin.left, y1: tickY, x2: width - margin.right, y2: tickY, class: "grid-line" }));
      svg.append(svgNode("text", { x: margin.left - 10, y: tickY + 4, "text-anchor": "end", class: "axis-label" }, formatAxis(tick, latest.unit)));
    }
    const labelIndexes = new Set([0, rows.length - 1]);
    const interval = Math.max(1, Math.floor((rows.length - 1) / 5));
    for (let index = interval; index < rows.length - 1; index += interval) labelIndexes.add(index);
    [...labelIndexes].sort((a, b) => a - b).forEach((index) => {
      svg.append(svgNode("text", { x: x(index), y: height - 18, "text-anchor": "middle", class: "axis-label" }, rows[index].period));
    });
    const coordinates = rows.map((row, index) => [x(index), y(Number(row.value))]);
    const path = coordinates.map(([px, py], index) => `${index === 0 ? "M" : "L"}${px.toFixed(2)},${py.toFixed(2)}`).join(" ");
    const area = `${path} L${coordinates[coordinates.length - 1][0].toFixed(2)},${(height - margin.bottom).toFixed(2)} L${coordinates[0][0].toFixed(2)},${(height - margin.bottom).toFixed(2)} Z`;
    svg.append(svgNode("path", { d: area, class: "trend-area" }));
    if (latest.benchmarkValue != null) {
      const medianY = y(Number(latest.benchmarkValue));
      if (medianY >= margin.top && medianY <= height - margin.bottom) svg.append(svgNode("line", { x1: margin.left, y1: medianY, x2: width - margin.right, y2: medianY, class: "median-line-svg" }));
    }
    svg.append(svgNode("path", { d: path, class: "trend-path" }));
    rows.forEach((row, index) => {
      const point = svgNode("circle", {
        cx: coordinates[index][0],
        cy: coordinates[index][1],
        r: index === rows.length - 1 ? 5.5 : 3.5,
        class: `trend-point${index === rows.length - 1 ? " is-latest" : ""}`,
        tabindex: "0",
        role: "img",
        "aria-label": `${formatPeriod(row.period)}: ${formatValue(row.value, row.unit, metric.displayPrecision)}`,
      });
      const tooltip = `<strong>${escapeHtml(formatPeriod(row.period))}</strong><br>${escapeHtml(formatValue(row.value, row.unit, metric.displayPrecision))}<br>${escapeHtml(row.yearChangePercent == null && metric.changeMethod !== "percentage_point" ? "No annual comparison" : formatYearChange({ ...row, changeMethod: metric.changeMethod, displayPrecision: metric.displayPrecision }))}`;
      point.addEventListener("mousemove", (event) => showTooltip(event, tooltip));
      point.addEventListener("mouseleave", hideTooltip);
      point.addEventListener("focus", (event) => showTooltip({ clientX: event.target.getBoundingClientRect().right, clientY: event.target.getBoundingClientRect().top }, tooltip));
      point.addEventListener("blur", hideTooltip);
      svg.append(point);
    });
    chart.replaceChildren(svg);

    const latestSignal = data.latest.find((row) => row.countyCode === state.countyCode && row.metricId === state.trendMetric);
    const signalCopy = latestSignal ? `${titleCase(latestSignal.trendSignal)} on a ${titleCase(latestSignal.signalBasis)} basis` : "No latest signal";
    $("#trend-callout").innerHTML = `<strong>Latest: ${escapeHtml(formatValue(latest.value, latest.unit, metric.displayPrecision))}</strong> in ${escapeHtml(formatPeriod(latest.period))}. ${escapeHtml(formatYearChange({ ...latest, changeMethod: metric.changeMethod, displayPrecision: metric.displayPrecision }))}. ${escapeHtml(signalCopy)}; this is a mechanical classification, not a causal finding.`;
    setText("#trend-description", `${metric.metricLabel} time series for ${county?.countyName}. The latest value is ${formatValue(latest.value, latest.unit, metric.displayPrecision)} in ${formatPeriod(latest.period)}.`);
    $("#trend-table-body").innerHTML = rows.slice(-8).reverse().map((row) => `<tr><th scope="row">${escapeHtml(formatPeriod(row.period))}</th><td>${escapeHtml(formatValue(row.value, row.unit, metric.displayPrecision))}</td><td>${escapeHtml(formatYearChange({ ...row, changeMethod: metric.changeMethod, displayPrecision: metric.displayPrecision }))}</td><td>${escapeHtml(titleCase(row.valueStatus))}</td></tr>`).join("");
  }

  function renderEducation() {
    const educationReadiness = data.readiness.filter((row) => row.indicatorId.startsWith("E")).sort((a, b) => a.indicatorId.localeCompare(b.indicatorId));
    const live = educationReadiness.reduce((sum, row) => sum + Number(row.currentObservationCount || 0), 0);
    setText("#education-live-count", compact(live, 1));
    setText("#education-status-label", live ? "Validated observations available" : "Awaiting validated sources");
    const stageCopy = {
      E1: { title: "Skills base", text: "What credentials and skills are present among adult residents?" },
      E2: { title: "Readiness & attendance", text: "Who completes high school ready for college, and who enrolls within 12 months?" },
      E3: { title: "Access through completion", text: "Who can enter, afford, persist, stop out, transfer, and graduate?" },
      E4: { title: "Outcomes by field", text: "What employment, unemployment, and earnings follow graduation by major?" },
    };
    $("#education-pathway").innerHTML = educationReadiness.map((row) => {
      const stage = stageCopy[row.indicatorId] || { title: row.indicatorName, text: row.readinessNote };
      const metrics = data.metrics.filter((metric) => metric.indicatorId === row.indicatorId);
      return `<article class="pathway-card">
        <div class="pathway-step"><span class="pathway-code">${escapeHtml(row.indicatorId)}</span>${readinessBadge(row.readinessStatus)}</div>
        <h3>${escapeHtml(stage.title)}</h3>
        <p>${escapeHtml(stage.text)}</p>
        <div class="pathway-metrics">${metrics.map((metric) => `<span class="metric-chip">${escapeHtml(metric.metricLabel)}</span>`).join("")}</div>
      </article>`;
    }).join("");

    const educationMetrics = data.metrics.filter((metric) => metric.indicatorId.startsWith("E"));
    $("#education-policy-body").innerHTML = educationMetrics.map((metric) => {
      const readiness = educationReadiness.find((row) => row.indicatorId === metric.indicatorId);
      const comparison = metric.comparisonBasis === "none" ? "No county rank" : metric.comparisonBasis === "year_change" ? "Annual change" : `Latest level · ${metric.rankDirection === "lower_is_better" ? "lower ranks stronger" : "higher ranks stronger"}`;
      return `<tr>
        <th scope="row">${escapeHtml(metric.indicatorId)}</th>
        <td>${escapeHtml(metric.metricLabel)}</td>
        <td>${escapeHtml(comparison)}</td>
        <td>${escapeHtml(metric.interpretationNote)}</td>
        <td>${readinessBadge(readiness?.readinessStatus || "planned")}</td>
      </tr>`;
    }).join("");
  }

  function renderHealth() {
    const passing = data.qualityChecks.filter((check) => check.passed).length;
    setText("#health-check-count", `${passing}/${data.qualityChecks.length}`);
    const freshness = data.overview.recencyCounts;
    const statuses = ["current", "delayed", "stale", "unknown"].filter((status) => freshness[status]);
    const max = Math.max(1, ...statuses.map((status) => freshness[status]));
    $("#freshness-chart").innerHTML = statuses.map((status) => `<div class="freshness-row"><span>${escapeHtml(freshnessLabel(status))}</span><div class="freshness-track"><div class="freshness-fill ${escapeHtml(status)}" style="width:${(freshness[status] / max) * 100}%"></div></div><span class="freshness-count">${escapeHtml(freshness[status])}</span></div>`).join("");
    setText("#freshness-note", `${data.overview.latestSeriesCount} latest county series are evaluated against frequency-specific age thresholds. Stale facts remain visible but cannot silently present as current.`);

    $("#indicator-freshness-body").innerHTML = data.indicatorFreshness.map((row) => `<tr>
      <th scope="row"><span class="indicator-code">${escapeHtml(row.indicatorId)}</span> ${escapeHtml(row.indicatorName)}</th>
      <td>${escapeHtml(formatPeriod(row.observationPeriod))}</td>
      <td>${escapeHtml(formatDate(row.publicationDate))}</td>
      <td>${escapeHtml(formatDate(row.retrievalDate))}</td>
      <td>${escapeHtml(titleCase(row.releaseStatus))}${row.coverageStatus === "active" ? ` · ${freshnessBadge(row.recencyStatus)}` : ""}</td>
      <td>${escapeHtml(formatDate(row.nextExpectedUpdate))}</td>
    </tr>`).join("");

    const statusCounts = ["active", "model_ready", "planned"].map((status) => ({ status, count: data.readiness.filter((row) => row.readinessStatus === status).length }));
    const total = data.readiness.length || 1;
    const activeEnd = (statusCounts[0].count / total) * 360;
    const modelEnd = activeEnd + (statusCounts[1].count / total) * 360;
    $("#readiness-donut").innerHTML = `<div class="donut-visual" style="background:conic-gradient(var(--positive) 0deg ${activeEnd}deg, var(--gold) ${activeEnd}deg ${modelEnd}deg, #b8bbb4 ${modelEnd}deg 360deg)"><div class="donut-center"><strong>${escapeHtml(total)}</strong><span>indicators</span></div></div><div class="donut-legend">${statusCounts.map((item) => `<div><span>${escapeHtml(readinessLabel(item.status))}</span><strong>${escapeHtml(item.count)}</strong></div>`).join("")}</div>`;

    const pillars = new Map();
    data.readiness.forEach((row) => {
      if (!pillars.has(row.pillar)) pillars.set(row.pillar, []);
      pillars.get(row.pillar).push(row);
    });
    $("#indicator-matrix").innerHTML = [...pillars.entries()].map(([pillar, rows]) => `<article class="pillar-card"><h3>${escapeHtml(pillar)}</h3><ul class="indicator-list">${rows.sort((a, b) => a.indicatorId.localeCompare(b.indicatorId)).map((row) => `<li class="indicator-item" title="${escapeHtml(readinessLabel(row.readinessStatus))}"><span class="indicator-code">${escapeHtml(row.indicatorId)}</span><span>${escapeHtml(row.indicatorName)}</span><i class="mini-state ${escapeHtml(row.readinessStatus)}" aria-label="${escapeHtml(readinessLabel(row.readinessStatus))}"></i></li>`).join("")}</ul></article>`).join("");

    $("#source-table-body").innerHTML = data.sources.map((source) => {
      const statusParts = Object.entries(source.recencyCounts).map(([status, count]) => `${count} ${freshnessLabel(status).toLowerCase()}`).join(" · ");
      const dominant = source.recencyCounts.stale ? "stale" : source.recencyCounts.delayed ? "delayed" : "current";
      return `<tr><th scope="row">${escapeHtml(source.sourceLabel)}</th><td>${escapeHtml(basisLabel(source.geographyBasis))}</td><td>${escapeHtml(formatPeriod(source.latestPeriod))}</td><td>${escapeHtml(source.seriesCount)}</td><td>${freshnessBadge(dominant)} <span>${escapeHtml(statusParts)}</span></td></tr>`;
    }).join("");
    setText("#benchmark-definition", `${data.meta.benchmarkDefinition}. It is a reference point, not a summed or population-weighted Bay Area aggregate.`);
    setText("#signal-caution", `${data.meta.signalCaution}. A signal prefers exact year-over-year movement, then exact period-over-period movement, and otherwise reports insufficient history.`);
    $("#quality-list").innerHTML = data.qualityChecks.map((check) => `<span class="quality-chip" title="${escapeHtml(check.message)}">${escapeHtml(titleCase(check.checkName))}</span>`).join("");
  }

  function initialize() {
    wireNavigation();
    populateSelectors();
    renderOverview();
    renderComparison();
    renderEducation();
    renderHealth();
    setView(state.view, { updateHash: false, scroll: false });
  }

  initialize();
})();
