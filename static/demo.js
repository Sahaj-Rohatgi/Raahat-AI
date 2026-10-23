(function () {
  var wrap = document.getElementById("wrap");
  var phone = document.getElementById("phone");
  var app = document.getElementById("app");
  var landscape = false;

  function fit() {
    var w = phone.offsetWidth, h = phone.offsetHeight;
    var s = Math.min((window.innerHeight - 32) / h, (window.innerWidth * (window.innerWidth > 900 ? 0.55 : 0.95)) / w, 1);
    phone.style.transformOrigin = "top left";
    phone.style.transform = "scale(" + s + ")";
    wrap.style.width = w * s + "px";
    wrap.style.height = h * s + "px";
  }
  document.getElementById("reload").addEventListener("click", function () { app.src = app.src; });
  document.getElementById("rotate").addEventListener("click", function () {
    landscape = !landscape;
    document.documentElement.style.setProperty("--w", landscape ? "844px" : "390px");
    document.documentElement.style.setProperty("--h", landscape ? "390px" : "844px");
    fit();
  });
  window.addEventListener("resize", fit);
  fit();
})();
