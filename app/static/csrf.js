// CSRF token (app/csrf.py): added to every POST form and every same-site
// fetch() that changes something, so no template or script has to remember.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  if (!meta) return;
  var token = meta.getAttribute("content");

  function isPost(form, submitter) {
    var method = (submitter && submitter.getAttribute("formmethod")) || form.getAttribute("method") || "get";
    return method.toLowerCase() === "post";
  }

  function addToken(form, submitter) {
    if (!isPost(form, submitter)) return;
    var input = form.querySelector('input[name="csrf_token"]');
    if (!input) {
      input = document.createElement("input");
      input.type = "hidden";
      input.name = "csrf_token";
      form.appendChild(input);
    }
    input.value = token;
  }

  document.addEventListener("submit", function (e) {
    if (e.target && e.target.tagName === "FORM") addToken(e.target, e.submitter);
  }, true);

  // form.submit() fires no submit event
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    addToken(this, null);
    return nativeSubmit.call(this);
  };

  var nativeFetch = window.fetch;
  if (nativeFetch) {
    window.fetch = function (input, init) {
      init = init || {};
      var isRequest = typeof Request !== "undefined" && input instanceof Request;
      var method = (init.method || (isRequest ? input.method : "GET")).toUpperCase();
      var url = new URL(isRequest ? input.url : String(input), window.location.href);
      if (method !== "GET" && method !== "HEAD" && url.origin === window.location.origin) {
        var headers = new Headers(init.headers || (isRequest ? input.headers : undefined));
        headers.set("X-CSRF-Token", token);
        init = Object.assign({}, init, { headers: headers });
      }
      return nativeFetch.call(this, input, init);
    };
  }
})();
