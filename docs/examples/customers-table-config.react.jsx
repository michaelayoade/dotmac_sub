import React, { useEffect, useMemo, useState } from "react";

function moveItem(items, fromIndex, toIndex) {
  const copy = [...items];
  const [moved] = copy.splice(fromIndex, 1);
  copy.splice(toIndex, 0, moved);
  return copy.map((item, index) => ({ ...item, display_order: index }));
}

export default function CustomersTable() {
  const [rows, setRows] = useState([]);
  const [columns, setColumns] = useState([]);
  const [isOpen, setIsOpen] = useState(false);
  const [isSaving, setIsSaving] = useState(false);
  const [dragIndex, setDragIndex] = useState(null);

  async function loadColumns() {
    const response = await fetch("/api/v1/tables/customers/columns");
    const payload = await response.json();
    setColumns(payload.columns);
  }

  async function loadData() {
    const response = await fetch("/api/v1/tables/customers/data?limit=50&offset=0");
    const payload = await response.json();
    setRows(payload.items);
    setColumns(payload.columns);
  }

  useEffect(() => {
    loadData();
  }, []);

  const visibleColumns = useMemo(
    () => [...columns].filter((column) => column.is_visible).sort((a, b) => a.display_order - b.display_order),
    [columns]
  );

  function toggleColumn(columnKey) {
    const next = columns.map((column) =>
      column.column_key === columnKey
        ? { ...column, is_visible: !column.is_visible }
        : column
    );
    setColumns(next);
  }

  function resetToDefault() {
    loadColumns();
  }

  async function saveColumns() {
    setIsSaving(true);
    const snapshot = [...columns];
    try {
      const response = await fetch("/api/v1/tables/customers/columns", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          columns.map((column, index) => ({
            column_key: column.column_key,
            display_order: index,
            is_visible: column.is_visible,
          }))
        ),
      });
      if (!response.ok) {
        throw new Error("save failed");
      }
      await loadData();
      setIsOpen(false);
    } catch (error) {
      setColumns(snapshot);
      console.error(error);
    } finally {
      setIsSaving(false);
    }
  }

  return (
    <div>
      <div style={{ marginBottom: 12 }}>
        <button onClick={() => setIsOpen(true)}>Configure Columns</button>
      </div>

      <table>
        <thead>
          <tr>
            {visibleColumns.map((column) => (
              <th key={column.column_key}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {visibleColumns.map((column) => (
                <td key={column.column_key}>{String(row[column.column_key] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>

      {isOpen && (
        <div role="dialog" aria-modal="true" style={{ border: "1px solid #ddd", padding: 16, marginTop: 16 }}>
          <h3>Column Selection</h3>
          {columns
            .slice()
            .sort((a, b) => a.display_order - b.display_order)
            .map((column, index) => (
              <div
                key={column.column_key}
                draggable
                onDragStart={() => setDragIndex(index)}
                onDragOver={(event) => event.preventDefault()}
                onDrop={() => {
                  if (dragIndex == null || dragIndex === index) {
                    return;
                  }
                  const ordered = columns.slice().sort((a, b) => a.display_order - b.display_order);
                  setColumns(moveItem(ordered, dragIndex, index));
                  setDragIndex(null);
                }}
                style={{ display: "flex", gap: 8, alignItems: "center", padding: 6 }}
              >
                <span style={{ cursor: "grab" }}>::</span>
                <input
                  type="checkbox"
                  checked={column.is_visible}
                  onChange={() => toggleColumn(column.column_key)}
                />
                <span>{column.label}</span>
              </div>
            ))}

          <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <button onClick={resetToDefault}>Reset to default</button>
            <button onClick={saveColumns} disabled={isSaving}>
              {isSaving ? "Saving..." : "Save"}
            </button>
            <button onClick={() => setIsOpen(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
