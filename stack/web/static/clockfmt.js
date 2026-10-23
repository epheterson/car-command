// The clock Settings chose, for Intl: the twin of clockfmt.py. 12-hour reads "3:05 PM"; 24-hour always has two digits, "03:05".
// Pass the result as the hour part of Intl.DateTimeFormat or toLocaleTimeString; `h12` is the site's own answer (true, false), never the browser's.
window.CCClock = {
  hour: function (h12) {
    if (h12 === false) return { hourCycle: "h23", hour: "2-digit" };
    if (h12 === true) return { hour12: true, hour: "numeric" };
    return { hour: "numeric" };
  },
  // A time of day as typed ("21:00", "9:30 pm", "9pm", "0930") into minutes since midnight, or null: the twin of clockfmt.parse_tod.
  parse: function (text) {
    var m = /^\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*([ap])?\.?m?\.?\s*$/i.exec(String(text));
    if (!m) return null;
    var h = +m[1], min = +(m[2] || 0), ap = (m[3] || "").toLowerCase();
    if (min > 59) return null;
    if (ap) { if (h < 1 || h > 12) return null; h = h % 12 + (ap === "p" ? 12 : 0); }
    else if (h > 23) return null;
    return h * 60 + min;
  }
};
