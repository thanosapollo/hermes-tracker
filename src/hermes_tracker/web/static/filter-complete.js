/* Token-aware datalist progressive enhancement for comma-separated filters. */
(function () {
  "use strict";

  function splitTokens(value) {
    return value
      .split(",")
      .map(function (part) {
        return part.trim();
      })
      .filter(Boolean);
  }

  function currentToken(value) {
    var comma = value.lastIndexOf(",");
    if (comma === -1) {
      return value.trim();
    }
    return value.slice(comma + 1).trim();
  }

  function prefixBeforeToken(value) {
    var comma = value.lastIndexOf(",");
    if (comma === -1) {
      return "";
    }
    var head = splitTokens(value.slice(0, comma));
    return head.length ? head.join(", ") + ", " : "";
  }

  function bindInput(input) {
    var listId = input.getAttribute("list");
    if (!listId) {
      return;
    }
    var datalist = document.getElementById(listId);
    if (!datalist) {
      return;
    }
    var options = Array.prototype.map.call(datalist.querySelectorAll("option"), function (option) {
      return option.value;
    });
    if (!options.length) {
      return;
    }

    function refresh() {
      var value = input.value;
      var selected = {};
      splitTokens(value).forEach(function (token) {
        selected[token] = true;
      });
      var token = currentToken(value);
      var tokenLower = token.toLowerCase();
      var prefix = prefixBeforeToken(value);
      while (datalist.firstChild) {
        datalist.removeChild(datalist.firstChild);
      }
      options.forEach(function (option) {
        if (selected[option] && option !== token) {
          return;
        }
        if (token && option.toLowerCase().indexOf(tokenLower) === -1) {
          return;
        }
        var node = document.createElement("option");
        node.value = prefix + option;
        datalist.appendChild(node);
      });
    }

    input.addEventListener("input", refresh);
    input.addEventListener("focus", refresh);
  }

  document.querySelectorAll("input[data-token-complete]").forEach(bindInput);
})();
