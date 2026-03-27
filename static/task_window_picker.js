(function () {
    var DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    var MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    var TASK_DAY_PREFIXES = [
        "task_window_day_0",
        "task_window_day_1",
        "task_window_day_2",
        "task_window_day_3",
        "task_window_day_4",
        "task_window_day_5",
        "task_window_day_6"
    ];

    function parseLocalDate(value) {
        if (!value) {
            return null;
        }
        var parts = value.split("-");
        if (parts.length !== 3) {
            return null;
        }
        var parsed = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
        if (Number.isNaN(parsed.getTime())) {
            return null;
        }
        return parsed;
    }

    function formatInputValue(value) {
        var month = String(value.getMonth() + 1).padStart(2, "0");
        var day = String(value.getDate()).padStart(2, "0");
        return value.getFullYear() + "-" + month + "-" + day;
    }

    function startOfWeek(value) {
        var copy = new Date(value.getFullYear(), value.getMonth(), value.getDate());
        var weekday = (copy.getDay() + 6) % 7;
        copy.setDate(copy.getDate() - weekday);
        return copy;
    }

    function addDays(value, days) {
        var copy = new Date(value.getFullYear(), value.getMonth(), value.getDate());
        copy.setDate(copy.getDate() + days);
        return copy;
    }

    function formatDayLabel(value) {
        return DAY_NAMES[(value.getDay() + 6) % 7] + " " + MONTH_NAMES[value.getMonth()] + " " + value.getDate();
    }

    function formatTimeLabel(value) {
        if (!value) {
            return "";
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

    function currentWeekStart(weekInput, scheduledDateInput) {
        return startOfWeek(parseLocalDate(weekInput.value) || parseLocalDate(scheduledDateInput.value) || new Date());
    }

    function selectedPrefix(root) {
        return TASK_DAY_PREFIXES.find(function (prefix) {
            var input = root.querySelector("#id_" + prefix + "_segments");
            return input && input.value && input.value !== "[]";
        }) || null;
    }

    function syncScheduledDate(root, weekStart, scheduledDateInput) {
        var prefix = selectedPrefix(root);
        if (!prefix) {
            scheduledDateInput.value = "";
            return;
        }
        var dayIndex = TASK_DAY_PREFIXES.indexOf(prefix);
        scheduledDateInput.value = formatInputValue(addDays(weekStart, dayIndex));
    }

    function updateDayLabels(root, weekStart) {
        var headers = root.querySelectorAll(".weekly-calendar-header");
        TASK_DAY_PREFIXES.forEach(function (prefix, index) {
            var currentDate = addDays(weekStart, index);
            var label = formatDayLabel(currentDate);
            if (headers[index]) {
                headers[index].textContent = label;
            }

            var card = root.querySelector('[data-schedule-summary-card="' + prefix + '"]');
            if (card) {
                card.dataset.calendarDate = formatInputValue(currentDate);
                var title = card.querySelector("h3");
                if (title) {
                    title.textContent = label;
                }
            }

            root.querySelectorAll('.weekly-calendar-cell[data-day="' + prefix + '"]').forEach(function (cell) {
                cell.dataset.calendarDate = formatInputValue(currentDate);
                var rangeLabel = formatTimeLabel(cell.dataset.slotValue) + " - " + formatTimeLabel(cell.dataset.slotEnd);
                cell.setAttribute("aria-label", label + " " + rangeLabel);
                cell.setAttribute("title", label + " " + rangeLabel);
            });
        });
    }

    function initTaskWindowPicker(root) {
        if (!root || root.dataset.taskWindowBound === "true") {
            return;
        }
        var weekInput = document.getElementById("id_scheduled_week_of");
        var scheduledDateInput = document.getElementById("id_scheduled_date");
        if (!weekInput || !scheduledDateInput) {
            return;
        }
        root.dataset.taskWindowBound = "true";

        function refreshWeek() {
            var weekStart = currentWeekStart(weekInput, scheduledDateInput);
            weekInput.value = formatInputValue(weekStart);
            updateDayLabels(root, weekStart);
            syncScheduledDate(root, weekStart, scheduledDateInput);
        }

        root.addEventListener("taskforge:schedule-change", function (event) {
            if (!event.detail || TASK_DAY_PREFIXES.indexOf(event.detail.day) === -1) {
                return;
            }
            syncScheduledDate(root, currentWeekStart(weekInput, scheduledDateInput), scheduledDateInput);
        });

        weekInput.addEventListener("change", refreshWeek);
        refreshWeek();
    }

    function initAllTaskWindowPickers() {
        initTaskWindowPicker(document.getElementById("task-window-picker"));
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAllTaskWindowPickers);
    } else {
        initAllTaskWindowPickers();
    }
})();
