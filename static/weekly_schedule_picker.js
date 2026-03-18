(function () {
    function formatTimeLabel(value) {
        if (!value) {
            return "Not scheduled";
        }
        var parts = value.split(":");
        if (parts.length !== 2) {
            return value;
        }
        var hour = parseInt(parts[0], 10);
        var minute = parts[1];
        var suffix = hour >= 12 ? "PM" : "AM";
        var hour12 = hour % 12;
        if (hour12 === 0) {
            hour12 = 12;
        }
        return hour12 + ":" + minute + " " + suffix;
    }

    function minutesBetween(startValue, endValue) {
        var startParts = startValue.split(":");
        var endParts = endValue.split(":");
        if (startParts.length !== 2 || endParts.length !== 2) {
            return 0;
        }
        var startMinutes = parseInt(startParts[0], 10) * 60 + parseInt(startParts[1], 10);
        var endMinutes = parseInt(endParts[0], 10) * 60 + parseInt(endParts[1], 10);
        return Math.max(0, endMinutes - startMinutes);
    }

    function formatDuration(startValue, endValue) {
        var totalMinutes = minutesBetween(startValue, endValue);
        if (!totalMinutes) {
            return "";
        }
        var hours = totalMinutes / 60;
        var label = Number.isInteger(hours) ? String(hours) : hours.toFixed(1).replace(/\.0$/, "");
        return label + " hr" + (label === "1" ? "" : "s");
    }

    function initPicker(root) {
        var states = {};
        var dragging = null;
        var frame = root.querySelector(".weekly-calendar-frame");

        root.querySelectorAll("[data-schedule-summary-card]").forEach(function (card) {
            var day = card.getAttribute("data-schedule-summary-card");
            var startSelect = root.querySelector("#id_" + day + "_start");
            var endSelect = root.querySelector("#id_" + day + "_end");
            var summary = root.querySelector('[data-schedule-summary-text="' + day + '"]');
            var clearButton = root.querySelector('[data-clear-day="' + day + '"]');
            states[day] = {
                card: card,
                startSelect: startSelect,
                endSelect: endSelect,
                summary: summary,
                cells: Array.from(root.querySelectorAll('.weekly-calendar-cell[data-day="' + day + '"]')),
            };
            if (clearButton) {
                clearButton.addEventListener("click", function () {
                    clearDay(day);
                });
            }
            if (startSelect) {
                startSelect.addEventListener("change", function () {
                    refreshDay(day);
                });
            }
            if (endSelect) {
                endSelect.addEventListener("change", function () {
                    refreshDay(day);
                });
            }
        });

        function getRange(day) {
            var state = states[day];
            if (!state || !state.startSelect || !state.endSelect) {
                return null;
            }
            var startValue = state.startSelect.value;
            var endValue = state.endSelect.value;
            if (!startValue || !endValue) {
                return null;
            }
            var startIndex = state.cells.findIndex(function (cell) {
                return cell.dataset.slotValue === startValue;
            });
            var endIndex = state.cells.findIndex(function (cell) {
                return cell.dataset.slotEnd === endValue;
            });
            if (startIndex === -1 || endIndex === -1 || endIndex < startIndex) {
                return null;
            }
            return {
                startValue: startValue,
                endValue: endValue,
                startIndex: startIndex,
                endIndex: endIndex,
            };
        }

        function refreshDay(day) {
            var state = states[day];
            if (!state) {
                return;
            }
            var startValue = state.startSelect ? state.startSelect.value : "";
            var endValue = state.endSelect ? state.endSelect.value : "";
            var range = getRange(day);
            var hasWindow = Boolean(startValue && endValue);
            state.card.classList.toggle("is-scheduled", hasWindow);
            if (state.summary) {
                if (hasWindow) {
                    var duration = formatDuration(startValue, endValue);
                    state.summary.textContent = formatTimeLabel(startValue) + " - " + formatTimeLabel(endValue) + (duration ? " (" + duration + ")" : "");
                } else {
                    state.summary.textContent = "Not scheduled";
                }
            }
            state.cells.forEach(function (cell, index) {
                var selected = Boolean(range && index >= range.startIndex && index <= range.endIndex);
                cell.classList.toggle("is-selected", selected);
                cell.setAttribute("aria-pressed", selected ? "true" : "false");
            });
        }

        function refreshAll() {
            Object.keys(states).forEach(function (day) {
                refreshDay(day);
            });
        }

        function clearDay(day) {
            var state = states[day];
            if (!state) {
                return;
            }
            if (state.startSelect) {
                state.startSelect.value = "";
            }
            if (state.endSelect) {
                state.endSelect.value = "";
            }
            refreshDay(day);
        }

        function setRange(day, firstIndex, secondIndex) {
            var state = states[day];
            if (!state || !state.startSelect || !state.endSelect) {
                return;
            }
            var startIndex = Math.min(firstIndex, secondIndex);
            var endIndex = Math.max(firstIndex, secondIndex);
            var startCell = state.cells[startIndex];
            var endCell = state.cells[endIndex];
            if (!startCell || !endCell) {
                return;
            }
            state.startSelect.value = startCell.dataset.slotValue;
            state.endSelect.value = endCell.dataset.slotEnd;
            refreshDay(day);
        }

        var clearWeekButton = root.querySelector("[data-clear-week]");
        if (clearWeekButton) {
            clearWeekButton.addEventListener("click", function () {
                Object.keys(states).forEach(function (day) {
                    clearDay(day);
                });
            });
        }

        root.querySelectorAll(".weekly-calendar-cell").forEach(function (cell) {
            cell.addEventListener("mousedown", function (event) {
                event.preventDefault();
                dragging = {
                    day: cell.dataset.day,
                    anchorIndex: Number(cell.dataset.slotIndex),
                };
                root.classList.add("is-dragging");
                setRange(dragging.day, dragging.anchorIndex, dragging.anchorIndex);
            });
            cell.addEventListener("mouseenter", function () {
                if (!dragging || dragging.day !== cell.dataset.day) {
                    return;
                }
                setRange(dragging.day, dragging.anchorIndex, Number(cell.dataset.slotIndex));
            });
        });

        document.addEventListener("mouseup", function () {
            if (!dragging) {
                return;
            }
            dragging = null;
            root.classList.remove("is-dragging");
        });

        refreshAll();

        if (frame) {
            var targetCell = root.querySelector(".weekly-calendar-cell.is-selected") || root.querySelector('.weekly-calendar-cell[data-slot-value="08:00"]');
            if (targetCell) {
                frame.scrollTop = Math.max(0, targetCell.offsetTop - 160);
            }
        }
    }

    function initAllPickers() {
        document.querySelectorAll("[data-weekly-schedule-picker]").forEach(function (root) {
            initPicker(root);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAllPickers);
    } else {
        initAllPickers();
    }
})();
