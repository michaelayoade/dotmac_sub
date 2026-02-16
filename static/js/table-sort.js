/**
 * Table column sorting — works with compact_filters and live_search forms.
 *
 * Clickable <th data-sort-column="..."> headers update hidden form inputs
 * named "sort" and "sort_dir", then trigger the HTMX search/filter cycle.
 */
(function () {
  // On page load, pre-populate hidden sort inputs from URL query params
  // so that filter/search changes preserve the active sort state.
  function seedSortInputs() {
    var params = new URLSearchParams(window.location.search);
    var sort = params.get("sort");
    var sortDir = params.get("sort_dir");
    if (!sort) return;

    document.querySelectorAll("form[hx-get], form[action]").forEach(function (form) {
      var sortInput = form.querySelector('input[name="sort"]');
      var dirInput  = form.querySelector('input[name="sort_dir"]');

      if (!sortInput) {
        sortInput = document.createElement("input");
        sortInput.type = "hidden";
        sortInput.name = "sort";
        form.appendChild(sortInput);
      }
      if (!dirInput) {
        dirInput = document.createElement("input");
        dirInput.type = "hidden";
        dirInput.name = "sort_dir";
        form.appendChild(dirInput);
      }

      sortInput.value = sort;
      dirInput.value  = sortDir || "desc";
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", seedSortInputs);
  } else {
    seedSortInputs();
  }

  document.addEventListener("click", function (e) {
    var th = e.target.closest("th[data-sort-column]");
    if (!th) return;

    var column = th.getAttribute("data-sort-column");
    // Find the nearest form (compact_filters or live_search)
    var card = th.closest("#results-container")
      || th.closest(".page-enter")
      || th.closest("main");
    if (!card) return;
    var form = card.querySelector("form[hx-get], form[action]");
    if (!form) return;

    // Read current sort state from the form
    var sortInput = form.querySelector('input[name="sort"]');
    var dirInput  = form.querySelector('input[name="sort_dir"]');

    if (!sortInput) {
      sortInput = document.createElement("input");
      sortInput.type = "hidden";
      sortInput.name = "sort";
      form.appendChild(sortInput);
    }
    if (!dirInput) {
      dirInput = document.createElement("input");
      dirInput.type = "hidden";
      dirInput.name = "sort_dir";
      form.appendChild(dirInput);
    }

    // Toggle direction if same column, else default to asc
    var newDir = "asc";
    if (sortInput.value === column) {
      newDir = dirInput.value === "asc" ? "desc" : "asc";
    }

    sortInput.value = column;
    dirInput.value  = newDir;

    // Trigger HTMX request — the search input has hx-include="closest form"
    var htmxEl = form.querySelector("[hx-get]");
    if (htmxEl && window.htmx) {
      htmx.trigger(htmxEl, "search");
    } else {
      form.submit();
    }
  });
})();
