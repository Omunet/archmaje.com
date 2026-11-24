(function () {
  var ACCOUNT = /^[a-z0-9_-]+$/;
  var IMAGE = /^\/assets\/instagram\/[a-z0-9_-]+\/\d+\.(?:jpg|jpeg|png|webp|gif)$/;

  function permalinkOk(url) {
    try {
      var parsed = new URL(url);
      return parsed.protocol === "https:" &&
        (parsed.hostname === "www.instagram.com" || parsed.hostname === "instagram.com");
    } catch (e) {
      return false;
    }
  }

  function render(section, posts) {
    var account = section.getAttribute("data-ig-account");
    var row = section.querySelector("[data-ig-row]");
    if (!row || !Array.isArray(posts) || !posts.length) return;
    posts.slice(0, 3).forEach(function (post) {
      if (!post || !permalinkOk(post.permalink) || !IMAGE.test(post.image)) return;
      if (post.image.indexOf("/assets/instagram/" + account + "/") !== 0) return;
      var link = document.createElement("a");
      link.href = post.permalink;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      var img = document.createElement("img");
      img.src = post.image;
      img.alt = post.caption ? String(post.caption) : "Instagram";
      img.loading = "lazy";
      link.appendChild(img);
      row.appendChild(link);
    });
    if (row.children.length) section.classList.add("has-posts");
  }

  document.querySelectorAll("[data-ig-account]").forEach(function (section) {
    var account = section.getAttribute("data-ig-account") || "";
    if (!ACCOUNT.test(account)) return;
    fetch("/assets/instagram/" + account + ".json", { cache: "no-cache" })
      .then(function (res) {
        if (!res.ok) throw new Error("missing");
        return res.json();
      })
      .then(function (posts) { render(section, posts); })
      .catch(function () { /* profile link stays, no placeholder posts */ });
  });
})();
