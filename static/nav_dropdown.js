// Drives any [data-nav-dropdown] menu in the nav bar: click its toggle to
// open, closing any other open dropdown first; outside click/Escape closes
// whichever is open.
(function () {
    function closeAll(except) {
        document.querySelectorAll('[data-nav-dropdown].is-open').forEach(function (dropdown) {
            if (dropdown === except) {
                return;
            }
            dropdown.classList.remove('is-open');
            var toggle = dropdown.querySelector('.nav-dropdown-toggle');
            if (toggle) {
                toggle.setAttribute('aria-expanded', 'false');
            }
        });
    }

    document.querySelectorAll('[data-nav-dropdown]').forEach(function (dropdown) {
        var toggle = dropdown.querySelector('.nav-dropdown-toggle');
        if (!toggle) {
            return;
        }
        toggle.addEventListener('click', function (event) {
            event.stopPropagation();
            var isOpen = dropdown.classList.contains('is-open');
            closeAll(dropdown);
            dropdown.classList.toggle('is-open', !isOpen);
            toggle.setAttribute('aria-expanded', String(!isOpen));
        });
        dropdown.addEventListener('click', function (event) {
            event.stopPropagation();
        });
    });

    document.addEventListener('click', function () {
        closeAll(null);
    });

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
            closeAll(null);
        }
    });
}());
