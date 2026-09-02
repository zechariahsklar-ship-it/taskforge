// Opens/closes the hamburger nav on mobile: toggled by its button, closed
// by an outside click, Escape, or resizing back to desktop width.
(function () {
    var toggle = document.getElementById('mobile-nav-toggle');
    var nav = document.getElementById('site-nav');

    if (!toggle || !nav) {
        return;
    }

    function closeNav() {
        nav.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
    }

    toggle.addEventListener('click', function (event) {
        event.stopPropagation();
        var isOpen = nav.classList.toggle('is-open');
        toggle.setAttribute('aria-expanded', String(isOpen));
    });

    nav.addEventListener('click', function (event) {
        event.stopPropagation();
    });

    document.addEventListener('click', function () {
        closeNav();
    });

    document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
            closeNav();
        }
    });

    window.addEventListener('resize', function () {
        if (window.innerWidth > 760) {
            closeNav();
        }
    });
}());
