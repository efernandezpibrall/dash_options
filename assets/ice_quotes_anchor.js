(function () {
    let retry = null;

    function scrollWhenMounted() {
        if (retry) window.clearInterval(retry);
        if (window.location.hash !== '#ice-quotes') return;
        let attempts = 0;
        function align() {
            const target = document.getElementById('ice-quotes');
            if (target && target.getBoundingClientRect().top > 140) {
                target.scrollIntoView({ block: 'start' });
            }
            attempts += 1;
            if (attempts >= 20) window.clearInterval(retry);
        }
        align();
        retry = window.setInterval(align, 500);
    }

    window.addEventListener('hashchange', scrollWhenMounted);
    document.addEventListener('DOMContentLoaded', scrollWhenMounted);
    scrollWhenMounted();
})();
