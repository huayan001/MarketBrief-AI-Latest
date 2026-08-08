(() => {
  const STORAGE_KEY = "mb_locale";
  const DEFAULT_LOCALE = "zh-CN";
  const SUPPORTED = ["zh-CN", "en"];

  function readStoredLocale() {
    try {
      const value = localStorage.getItem(STORAGE_KEY);
      if (SUPPORTED.includes(value)) return value;
    } catch {
      /* ignore */
    }
    return DEFAULT_LOCALE;
  }

  let currentLocale = readStoredLocale();

  function catalog(locale) {
    const all = window.MB_LOCALES || {};
    return all[locale] || {};
  }

  function interpolate(template, vars) {
    if (!vars) return template;
    return String(template).replace(/\{(\w+)\}/g, (_, key) =>
      vars[key] === undefined || vars[key] === null ? `{${key}}` : String(vars[key]),
    );
  }

  function t(key, vars) {
    const primary = catalog(currentLocale)[key];
    const fallback = catalog(DEFAULT_LOCALE)[key];
    const text = primary ?? fallback ?? key;
    return interpolate(text, vars);
  }

  function getLocale() {
    return currentLocale;
  }

  function dateLocale() {
    return currentLocale === "en" ? "en-US" : "zh-CN";
  }

  function applyDomTranslations(root = document) {
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (key) el.textContent = t(key);
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      if (key) el.setAttribute("placeholder", t(key));
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      const key = el.getAttribute("data-i18n-title");
      if (key) el.setAttribute("title", t(key));
    });
    root.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      const key = el.getAttribute("data-i18n-aria");
      if (key) el.setAttribute("aria-label", t(key));
    });
    document.querySelectorAll("[data-locale-btn]").forEach((btn) => {
      const locale = btn.getAttribute("data-locale-btn");
      btn.classList.toggle("active", locale === currentLocale);
      btn.setAttribute("aria-pressed", locale === currentLocale ? "true" : "false");
    });
  }

  function setLocale(locale) {
    if (!SUPPORTED.includes(locale)) locale = DEFAULT_LOCALE;
    currentLocale = locale;
    try {
      localStorage.setItem(STORAGE_KEY, locale);
    } catch {
      /* ignore */
    }
    document.documentElement.lang = locale === "en" ? "en" : "zh-CN";
    applyDomTranslations();
    if (typeof window.rerenderI18n === "function") {
      window.rerenderI18n();
    }
  }

  function bindLocaleSwitchers(root = document) {
    root.querySelectorAll("[data-locale-btn]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const locale = btn.getAttribute("data-locale-btn");
        if (locale) setLocale(locale);
      });
    });
  }

  window.t = t;
  window.getLocale = getLocale;
  window.setLocale = setLocale;
  window.dateLocale = dateLocale;
  window.applyDomTranslations = applyDomTranslations;
  window.bindLocaleSwitchers = bindLocaleSwitchers;

  document.documentElement.lang = currentLocale === "en" ? "en" : "zh-CN";
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      applyDomTranslations();
      bindLocaleSwitchers();
    });
  } else {
    applyDomTranslations();
    bindLocaleSwitchers();
  }
})();
