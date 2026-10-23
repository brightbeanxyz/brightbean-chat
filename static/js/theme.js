// Light/dark theme. The choice ('light' or 'dark') lives in localStorage;
// no entry means "follow the OS". Loaded synchronously in <head>, so
// data-theme is on <html> before first paint and nothing flashes. The error
// pages (layouts/error.html) carry no script by design and stay light.
(function () {
    var media = window.matchMedia('(prefers-color-scheme: dark)');

    function stored() {
        try { return localStorage.getItem('theme'); } catch (e) { return null; }
    }

    function apply() {
        var choice = stored();
        var dark = choice === 'dark' || (choice !== 'light' && media.matches);
        document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    }

    window.bbTheme = {
        get: function () {
            var choice = stored();
            return choice === 'light' || choice === 'dark' ? choice : 'system';
        },
        set: function (choice) {
            try {
                if (choice === 'system') { localStorage.removeItem('theme'); }
                else { localStorage.setItem('theme', choice); }
            } catch (e) { /* storage disabled: applies to this page only */ }
            apply();
        }
    };

    apply();
    media.addEventListener('change', apply);
    // Another tab changed the choice.
    window.addEventListener('storage', function (e) { if (e.key === 'theme') { apply(); } });
})();
