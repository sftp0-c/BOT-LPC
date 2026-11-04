(function () {
  "use strict";
  var quiet = window.matchMedia("(prefers-reduced-motion: reduce)");
  var reveals = document.querySelectorAll(".lp-reveal");
  if (!quiet.matches && "IntersectionObserver" in window) {
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("is-visible");
          observer.unobserve(entry.target);
        }
      });
    }, {threshold: 0.12, rootMargin: "0px 0px -24px 0px"});
    reveals.forEach(function (element) { observer.observe(element); });
  }

  var gallery = document.querySelector(".lp-gallery");
  var controls = document.querySelector(".lp-gallery-controls");
  if (gallery && controls) {
    var previous = controls.querySelector("[data-gallery-prev]");
    var next = controls.querySelector("[data-gallery-next]");
    var update = function () {
      previous.disabled = gallery.scrollLeft <= 2;
      next.disabled = gallery.scrollLeft + gallery.clientWidth >= gallery.scrollWidth - 2;
    };
    var move = function (direction) {
      var screen = gallery.querySelector(".lp-screen");
      var width = screen ? screen.getBoundingClientRect().width : gallery.clientWidth;
      var gap = parseFloat(window.getComputedStyle(gallery).gap) || 0;
      gallery.scrollBy({left: direction * (width + gap), behavior: quiet.matches ? "instant" : "smooth"});
    };
    previous.addEventListener("click", function () { move(-1); });
    next.addEventListener("click", function () { move(1); });
    gallery.addEventListener("scroll", update, {passive: true});
    window.addEventListener("resize", update, {passive: true});
    controls.hidden = false;
    update();
  }

  var progress = document.querySelector(".lp-scroll");
  if (progress) {
    var scheduled = false;
    var measure = function () {
      var height = document.documentElement.scrollHeight - window.innerHeight;
      progress.value = height > 0 ? Math.min(100, Math.max(0, window.scrollY / height * 100)) : 0;
      scheduled = false;
    };
    window.addEventListener("scroll", function () {
      if (!scheduled) { scheduled = true; window.requestAnimationFrame(measure); }
    }, {passive: true});
    window.addEventListener("resize", measure, {passive: true});
    measure();
  }
})();
