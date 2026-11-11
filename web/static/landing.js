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

  // Compact sticky header and section-aware navigation.
  var header = document.querySelector(".lp-head");
  var navLinks = Array.prototype.slice.call(document.querySelectorAll('.lp-head nav a[href^="#"]'));
  var sections = navLinks.map(function (link) { return document.querySelector(link.getAttribute("href")); }).filter(Boolean);
  var updateHeader = function () { if (header) header.classList.toggle("is-scrolled", window.scrollY > 12); };
  updateHeader();
  window.addEventListener("scroll", updateHeader, {passive:true});
  if (!quiet.matches && "IntersectionObserver" in window && navLinks.length && sections.length) {
    var sectionObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        navLinks.forEach(function (link) {
          if (link.getAttribute("href") === "#" + entry.target.id) link.setAttribute("aria-current", "location");
          else link.removeAttribute("aria-current");
        });
      });
    }, {rootMargin:"-25% 0px -60% 0px",threshold:0});
    sections.forEach(function (section) { sectionObserver.observe(section); });
  }
  // Reveal cards as they enter the viewport; content remains visible without JS.
  if (!quiet.matches && "IntersectionObserver" in window) {
    var targets = document.querySelectorAll(".lp-feature,.lp-screen,.lp-roadmap article,.lp-flow li");
    var cardObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) { entry.target.classList.add("is-visible"); cardObserver.unobserve(entry.target); }
      });
    }, {threshold:0.08,rootMargin:"0px 0px -30px 0px"});
    targets.forEach(function (item) { cardObserver.observe(item); });
  }
})();
