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

    function formatHours(totalMinutes) {
        if (!totalMinutes) {
            return "";
        }
        var hours = totalMinutes / 60;
        var label = Number.isInteger(hours) ? String(hours) : hours.toFixed(1).replace(/\.0$/, "");
        return label + " hr" + (label === "1" ? "" : "s");
    }

    function parseSegments(rawValue) {
        if (!rawValue) {
            return [];
        }
        try {
            var payload = JSON.parse(rawValue);
            if (!Array.isArray(payload)) {
                return [];
            }
            return payload.filter(function (item) {
                return Array.isArray(item) && item.length === 2 && item[0] && item[1];
            });
        } catch (error) {
            return [];
        }
    }

    function cloneSegments(segments) {
        return segments.map(function (segment) {
            return [segment[0], segment[1]];
        });
    }

    function contiguousSegmentsFromSelection(state, selectedSet) {
        var sorted = Array.from(selectedSet).sort(function (left, right) {
            return left - right;
        });
        if (!sorted.length) {
            return [];
        }
        var segments = [];
        var segmentStart = sorted[0];
        var previous = sorted[0];
        for (var index = 1; index < sorted.length; index += 1) {
            var current = sorted[index];
            if (current === previous + 1) {
                previous = current;
                continue;
            }
            segments.push([
                state.cells[segmentStart].dataset.slotValue,
                state.cells[previous].dataset.slotEnd,
            ]);
            segmentStart = current;
            previous = current;
        }
        segments.push([
            state.cells[segmentStart].dataset.slotValue,
            state.cells[previous].dataset.slotEnd,
        ]);
        return segments;
    }

    function selectedSetFromSegments(state, segments) {
        var selected = new Set();
        segments.forEach(function (segment) {
            var startValue = segment[0];
            var endValue = segment[1];
            state.cells.forEach(function (cell, index) {
                if (cell.dataset.slotValue >= startValue && cell.dataset.slotEnd <= endValue) {
                    selected.add(index);
                }
            });
        });
        return selected;
    }

    function updateLegacyFields(state, segments) {
        var startInput = state.startInput;
        var endInput = state.endInput;
        var hoursInput = state.hoursInput;
        if (!segments.length) {
            if (startInput) {
                startInput.value = "";
            }
            if (endInput) {
                endInput.value = "";
            }
            if (hoursInput) {
                hoursInput.value = "0";
            }
            return;
        }
        var totalMinutes = segments.reduce(function (sum, segment) {
            return sum + minutesBetween(segment[0], segment[1]);
        }, 0);
        if (startInput) {
            startInput.value = segments[0][0];
        }
        if (endInput) {
            endInput.value = segments[segments.length - 1][1];
        }
        if (hoursInput) {
            hoursInput.value = String(totalMinutes / 60);
        }
    }

    function weekdayIndexFromDate(value) {
        if (!value) {
            return null;
        }
        var parsed = new Date(value + "T00:00:00");
        if (Number.isNaN(parsed.getTime())) {
            return null;
        }
        return (parsed.getDay() + 6) % 7;
    }

    function initPicker(root) {
        if (root.dataset.pickerBound === "true") {
            return;
        }
        root.dataset.pickerBound = "true";

        var states = {};
        var dragging = null;
        var frame = root.querySelector(".weekly-calendar-frame");

        root.querySelectorAll("[data-schedule-summary-card]").forEach(function (card) {
            var day = card.getAttribute("data-schedule-summary-card");
            var segmentsInput = root.querySelector("#id_" + day + "_segments");
            states[day] = {
                card: card,
                weekday: card.dataset.weekday,
                segmentsInput: segmentsInput,
                startInput: root.querySelector("#id_" + day + "_start"),
                endInput: root.querySelector("#id_" + day + "_end"),
                hoursInput: root.querySelector("#id_" + day + "_hours"),
                summary: root.querySelector('[data-schedule-summary-text="' + day + '"]'),
                cells: Array.from(root.querySelectorAll('.weekly-calendar-cell[data-day="' + day + '"]')),
                selectedIndices: new Set(),
            };
            var clearButton = root.querySelector('[data-clear-day="' + day + '"]');
            if (clearButton) {
                clearButton.addEventListener("click", function () {
                    setSegments(day, []);
                });
            }
        });

        function readSegments(day) {
            var state = states[day];
            if (!state || !state.segmentsInput) {
                return [];
            }
            return parseSegments(state.segmentsInput.value);
        }

        function setSegments(day, segments) {
            var state = states[day];
            if (!state || !state.segmentsInput) {
                return;
            }
            state.segmentsInput.value = JSON.stringify(segments);
            updateLegacyFields(state, segments);
            refreshDay(day);
            root.dispatchEvent(new CustomEvent("taskforge:schedule-change", {
                detail: {
                    day: day,
                    segments: cloneSegments(segments),
                },
            }));
        }

        function refreshDay(day) {
            var state = states[day];
            if (!state) {
                return;
            }
            var segments = readSegments(day);
            var selectedIndices = selectedSetFromSegments(state, segments);
            state.selectedIndices = selectedIndices;
            state.card.classList.toggle("is-scheduled", segments.length > 0);
            if (state.summary) {
                if (segments.length) {
                    var labels = segments.map(function (segment) {
                        return formatTimeLabel(segment[0]) + " - " + formatTimeLabel(segment[1]);
                    });
                    var totalMinutes = segments.reduce(function (sum, segment) {
                        return sum + minutesBetween(segment[0], segment[1]);
                    }, 0);
                    var durationLabel = formatHours(totalMinutes);
                    state.summary.textContent = labels.join(", ") + (durationLabel ? " (" + durationLabel + ")" : "");
                } else {
                    state.summary.textContent = "Not scheduled";
                }
            }
            state.cells.forEach(function (cell, index) {
                var selected = selectedIndices.has(index);
                cell.classList.toggle("is-selected", selected);
                cell.setAttribute("aria-pressed", selected ? "true" : "false");
            });
        }

        function refreshAll() {
            Object.keys(states).forEach(function (day) {
                refreshDay(day);
            });
        }

        function applySelection(day, baseSelected, anchorIndex, currentIndex, mode) {
            var state = states[day];
            if (!state) {
                return;
            }
            var startIndex = Math.min(anchorIndex, currentIndex);
            var endIndex = Math.max(anchorIndex, currentIndex);
            var selected = new Set(baseSelected);
            for (var index = startIndex; index <= endIndex; index += 1) {
                if (mode === "remove") {
                    selected.delete(index);
                } else {
                    selected.add(index);
                }
            }
            setSegments(day, contiguousSegmentsFromSelection(state, selected));
        }

        function pointerCellFromEvent(event) {
            var target = document.elementFromPoint(event.clientX, event.clientY);
            if (!target || typeof target.closest !== "function") {
                return null;
            }
            var cell = target.closest(".weekly-calendar-cell");
            if (!cell || !root.contains(cell)) {
                return null;
            }
            return cell;
        }

        function updatePointerSelection(event) {
            if (!dragging) {
                return;
            }
            var cell = pointerCellFromEvent(event);
            if (!cell || dragging.day !== cell.dataset.day) {
                return;
            }
            var index = Number(cell.dataset.slotIndex);
            if (dragging.lastIndex === index) {
                return;
            }
            dragging.lastIndex = index;
            applySelection(dragging.day, dragging.baseSelected, dragging.anchorIndex, index, dragging.mode);
        }

        function startPointerSelection(cell, event) {
            event.preventDefault();
            var day = cell.dataset.day;
            var state = states[day];
            if (!state) {
                return;
            }
            var index = Number(cell.dataset.slotIndex);
            dragging = {
                day: day,
                anchorIndex: index,
                lastIndex: index,
                mode: state.selectedIndices.has(index) ? "remove" : "add",
                baseSelected: new Set(state.selectedIndices),
                pointerId: event.pointerId,
            };
            root.classList.add("is-dragging");
            applySelection(day, dragging.baseSelected, index, index, dragging.mode);
        }

        function stopPointerSelection(pointerId) {
            if (!dragging) {
                return;
            }
            if (pointerId !== undefined && dragging.pointerId !== undefined && dragging.pointerId !== pointerId) {
                return;
            }
            dragging = null;
            root.classList.remove("is-dragging");
        }

        function weekdayTargetDays(sourceDay, scope) {
            return Object.keys(states).filter(function (day) {
                if (day === sourceDay) {
                    return false;
                }
                if (scope === "all") {
                    return true;
                }
                var weekdayValue = Number(states[day].weekday);
                return !Number.isNaN(weekdayValue) && weekdayValue < 5;
            });
        }

        var clearWeekButton = root.querySelector("[data-clear-week]");
        if (clearWeekButton) {
            clearWeekButton.addEventListener("click", function () {
                Object.keys(states).forEach(function (day) {
                    setSegments(day, []);
                });
            });
        }

        root.querySelectorAll("[data-copy-day]").forEach(function (button) {
            button.addEventListener("click", function () {
                var sourceDay = button.dataset.copyDay;
                var scope = button.dataset.copyScope;
                var segments = cloneSegments(readSegments(sourceDay));
                weekdayTargetDays(sourceDay, scope).forEach(function (targetDay) {
                    setSegments(targetDay, cloneSegments(segments));
                });
            });
        });

        root.querySelectorAll(".weekly-calendar-cell").forEach(function (cell) {
            cell.addEventListener("pointerdown", function (event) {
                startPointerSelection(cell, event);
            });
            cell.addEventListener("pointerenter", function (event) {
                updatePointerSelection(event);
            });
            cell.addEventListener("pointerup", function (event) {
                stopPointerSelection(event.pointerId);
            });
            cell.addEventListener("pointercancel", function (event) {
                stopPointerSelection(event.pointerId);
            });
        });

        document.addEventListener("pointermove", function (event) {
            if (!dragging) {
                return;
            }
            if (event.buttons === 0) {
                stopPointerSelection(event.pointerId);
                return;
            }
            updatePointerSelection(event);
        });
        document.addEventListener("pointerup", function (event) {
            stopPointerSelection(event.pointerId);
        });
        document.addEventListener("pointercancel", function (event) {
            stopPointerSelection(event.pointerId);
        });

        root.taskforgeSchedulePicker = {
            getSegments: function (day) {
                return cloneSegments(readSegments(day));
            },
            setSegments: function (day, segments) {
                setSegments(day, cloneSegments(segments || []));
            },
            getDayForWeekday: function (weekday) {
                return Object.keys(states).find(function (day) {
                    return String(states[day].weekday) === String(weekday);
                }) || null;
            },
        };

        refreshAll();

        if (frame) {
            var targetCell = root.querySelector(".weekly-calendar-cell.is-selected") || root.querySelector('.weekly-calendar-cell[data-slot-value="07:00"]');
            if (targetCell) {
                frame.scrollTop = Math.max(0, targetCell.offsetTop - 160);
            }
        }
    }

    function initOverrideTools() {
        document.querySelectorAll("[data-load-normal-schedule]").forEach(function (button) {
            if (button.dataset.overrideBound === "true") {
                return;
            }
            button.dataset.overrideBound = "true";
            var sourceRoot = document.getElementById(button.dataset.sourcePicker);
            var targetRoot = document.getElementById(button.dataset.targetPicker);
            var dateInput = document.getElementById(button.dataset.dateInput);
            if (!sourceRoot || !targetRoot || !dateInput) {
                return;
            }

            function loadSchedule(force) {
                var sourcePicker = sourceRoot.taskforgeSchedulePicker;
                var targetPicker = targetRoot.taskforgeSchedulePicker;
                if (!sourcePicker || !targetPicker) {
                    return;
                }
                var weekday = weekdayIndexFromDate(dateInput.value);
                if (weekday === null) {
                    return;
                }
                var sourceDay = sourcePicker.getDayForWeekday(weekday);
                if (!sourceDay) {
                    return;
                }
                var currentSegments = targetPicker.getSegments("override");
                if (!force && currentSegments.length) {
                    return;
                }
                targetPicker.setSegments("override", sourcePicker.getSegments(sourceDay));
            }

            button.addEventListener("click", function () {
                loadSchedule(true);
            });
            dateInput.addEventListener("change", function () {
                loadSchedule(false);
            });
        });
    }

    function initAllPickers() {
        document.querySelectorAll("[data-weekly-schedule-picker]").forEach(function (root) {
            initPicker(root);
        });
        initOverrideTools();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAllPickers);
    } else {
        initAllPickers();
    }
})();

