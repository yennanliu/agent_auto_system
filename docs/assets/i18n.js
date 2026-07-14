/*
 * Lightweight i18n toggle for the static site.
 *
 * How it works:
 *  - English text lives directly in the HTML (source of truth for SEO + editing).
 *  - Each translatable element carries data-i18n="key".
 *  - A page defines window.I18N_ZH = { key: "<html>", ... } before loading this file.
 *  - On load we cache the original English innerHTML, then swap to the saved language.
 *  - The chosen language is persisted in localStorage and shared across pages.
 *
 * Attribute translations (e.g. <title>, alt, aria-label) use:
 *    data-i18n-attr="attrName:key;attrName:key"
 */
(function () {
  "use strict";
  var STORAGE_KEY = "aas-lang";
  var original = new Map();
  var originalAttrs = new Map();
  var currentLang = null;

  function supported(lang) {
    return lang === "en" || lang === "zh";
  }

  function getLang() {
    var stored = null;
    try { stored = localStorage.getItem(STORAGE_KEY); } catch (e) {}
    if (supported(stored)) return stored;
    // First visit: honour the browser's preference for Chinese, else English.
    var nav = (navigator.language || "").toLowerCase();
    return nav.indexOf("zh") === 0 ? "zh" : "en";
  }

  function saveLang(lang) {
    try { localStorage.setItem(STORAGE_KEY, lang); } catch (e) {}
  }

  function cacheOriginals() {
    document.querySelectorAll("[data-i18n]").forEach(function (el) {
      if (!original.has(el)) original.set(el, el.innerHTML);
    });
    document.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
      if (originalAttrs.has(el)) return;
      var map = {};
      el.getAttribute("data-i18n-attr").split(";").forEach(function (pair) {
        var idx = pair.indexOf(":");
        if (idx === -1) return;
        var attr = pair.substring(0, idx).trim();
        if (attr) map[attr] = el.getAttribute(attr);
      });
      originalAttrs.set(el, map);
    });
  }

  function apply(lang) {
    if (!supported(lang)) lang = "en";
    if (lang === currentLang) return; // already applied — nothing to do
    var zh = window.I18N_ZH || {};

    // On the very first load in English, the DOM already holds the English
    // source, so skip the redundant innerHTML/attribute rewrites entirely.
    if (currentLang !== null || lang !== "en") {
      document.querySelectorAll("[data-i18n]").forEach(function (el) {
        var key = el.getAttribute("data-i18n");
        if (lang === "zh" && zh[key] != null) {
          el.innerHTML = zh[key];
        } else if (original.has(el)) {
          el.innerHTML = original.get(el);
        }
      });

      document.querySelectorAll("[data-i18n-attr]").forEach(function (el) {
        var spec = el.getAttribute("data-i18n-attr");
        var base = originalAttrs.get(el) || {};
        spec.split(";").forEach(function (pair) {
          var idx = pair.indexOf(":");
          if (idx === -1) return;
          var attr = pair.substring(0, idx).trim();
          var key = pair.substring(idx + 1).trim();
          if (!attr || !key) return;
          if (lang === "zh" && zh[key] != null) {
            el.setAttribute(attr, zh[key]);
          } else if (base[attr] != null) {
            el.setAttribute(attr, base[attr]);
          } else {
            // Attribute did not exist in the original — drop it on revert.
            el.removeAttribute(attr);
          }
        });
      });
    }

    document.documentElement.lang = lang === "zh" ? "zh-Hant" : "en";

    document.querySelectorAll("[data-lang-btn]").forEach(function (btn) {
      var active = btn.getAttribute("data-lang-btn") === lang;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
    });

    saveLang(lang);
    currentLang = lang;
  }

  function init() {
    cacheOriginals();
    document.querySelectorAll("[data-lang-btn]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        apply(btn.getAttribute("data-lang-btn"));
      });
    });
    apply(getLang());
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
