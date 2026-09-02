// Toggles a [data-task-window-toggle] button's paired [data-task-window-fields]
// panel open/closed, syncing aria-expanded. Used by the "Scheduled work
// window" panel on the Create/Edit Task and Recurring Task pages.
(function () {
    function initToggle(toggle) {
        var fields = document.querySelector('[data-task-window-fields]');
        if (!fields) {
            return;
        }
        toggle.addEventListener('click', function () {
            var expanded = toggle.getAttribute('aria-expanded') === 'true';
            toggle.setAttribute('aria-expanded', expanded ? 'false' : 'true');
            // Hiding the section only changes visibility, so the selected time(s) stay in place.
            fields.style.display = expanded ? 'none' : '';
        });
    }

    document.querySelectorAll('[data-task-window-toggle]').forEach(initToggle);
}());
