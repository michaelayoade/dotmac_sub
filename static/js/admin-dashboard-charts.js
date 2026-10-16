(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const SERIES = [
    { key: "breached_sla", label: "Breached SLA", color: "#f43f5e", shape: "circle" },
    { key: "approaching_sla", label: "Close to SLA", color: "#f59e0b", shape: "diamond" },
    { key: "newly_created", label: "Newly created", color: "#3b82f6", shape: "square" },
  ];

  function svgElement(tag, attributes) {
    const element = document.createElementNS(SVG_NS, tag);
    Object.entries(attributes || {}).forEach(([name, value]) =>
      element.setAttribute(name, String(value)),
    );
    return element;
  }

  function renderChart(root, selectedKey) {
    const dataNode = root.querySelector("[data-ticket-performance-data]");
    const chart = root.querySelector("[data-ticket-chart]");
    if (!dataNode || !chart) return;

    let datasets;
    try {
      datasets = JSON.parse(dataNode.textContent || "[]");
    } catch (_error) {
      return;
    }
    const dataset = datasets.find((item) => item.key === selectedKey) || datasets[0];
    if (!dataset || !Array.isArray(dataset.points) || dataset.points.length === 0) return;

    const width = 560;
    const height = 220;
    const inset = { top: 18, right: 16, bottom: 34, left: 34 };
    const plotWidth = width - inset.left - inset.right;
    const plotHeight = height - inset.top - inset.bottom;
    const values = dataset.points.flatMap((point) =>
      SERIES.map((series) => Number(point[series.key]) || 0),
    );
    const maximum = Math.max(1, ...values);
    const ceiling = Math.max(4, Math.ceil(maximum / 4) * 4);
    const x = (index) =>
      inset.left + (dataset.points.length === 1 ? plotWidth / 2 : (index / (dataset.points.length - 1)) * plotWidth);
    const y = (value) => inset.top + plotHeight - (value / ceiling) * plotHeight;

    chart.replaceChildren();
    const title = svgElement("title", { id: "ticket-performance-chart-title" });
    title.textContent = `Ticket performance for ${dataset.label} over the last seven days`;
    chart.appendChild(title);

    for (let index = 0; index <= 4; index += 1) {
      const value = Math.round((ceiling / 4) * index);
      const gridY = y(value);
      chart.appendChild(svgElement("line", { x1: inset.left, x2: width - inset.right, y1: gridY, y2: gridY, stroke: "currentColor", "stroke-opacity": "0.11", "stroke-width": "1" }));
      const label = svgElement("text", { x: inset.left - 9, y: gridY + 4, "text-anchor": "end", fill: "currentColor", "fill-opacity": "0.55", "font-size": "10" });
      label.textContent = String(value);
      chart.appendChild(label);
    }

    dataset.points.forEach((point, index) => {
      const label = svgElement("text", { x: x(index), y: height - 10, "text-anchor": "middle", fill: "currentColor", "fill-opacity": "0.58", "font-size": "10" });
      label.textContent = point.label;
      chart.appendChild(label);
    });

    SERIES.forEach((series) => {
      const coordinates = dataset.points.map((point, index) => ({ x: x(index), y: y(Number(point[series.key]) || 0), value: Number(point[series.key]) || 0 }));
      chart.appendChild(svgElement("polyline", { points: coordinates.map((point) => `${point.x},${point.y}`).join(" "), fill: "none", stroke: series.color, "stroke-width": "3", "stroke-linecap": "round", "stroke-linejoin": "round" }));
      coordinates.forEach((point) => {
        let marker;
        if (series.shape === "diamond") {
          marker = svgElement("rect", { x: point.x - 3.5, y: point.y - 3.5, width: 7, height: 7, rx: 1, fill: series.color, transform: `rotate(45 ${point.x} ${point.y})` });
        } else if (series.shape === "square") {
          marker = svgElement("rect", { x: point.x - 3.5, y: point.y - 3.5, width: 7, height: 7, rx: 1, fill: series.color });
        } else {
          marker = svgElement("circle", { cx: point.x, cy: point.y, r: 4, fill: series.color });
        }
        const markerTitle = svgElement("title");
        markerTitle.textContent = `${series.label}: ${point.value}`;
        marker.appendChild(markerTitle);
        chart.appendChild(marker);
      });
    });

    const totals = SERIES.map((series) => ({
      label: series.label,
      value: dataset.points.reduce((sum, point) => sum + (Number(point[series.key]) || 0), 0),
    }));
    const summary = root.querySelector("[data-ticket-chart-summary]");
    if (summary) summary.textContent = `${dataset.label}: ${totals.map((item) => `${item.value} ${item.label.toLowerCase()}`).join(", ")}.`;
  }

  function initialise(root) {
    const select = root.querySelector("[data-ticket-team]");
    renderChart(root, select ? select.value : "all");
    if (select && !select.dataset.chartBound) {
      select.dataset.chartBound = "true";
      select.addEventListener("change", () => renderChart(root, select.value));
    }
  }

  function initialiseAll(container) {
    (container || document).querySelectorAll("[data-ticket-performance]").forEach(initialise);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => initialiseAll(document));
  } else {
    initialiseAll(document);
  }
  document.addEventListener("htmx:afterSwap", (event) => initialiseAll(event.target));
})();
