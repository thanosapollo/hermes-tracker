/* Token-aware datalist progressive enhancement for comma-separated filters. */
(function (root) {
  "use strict";

  function splitTokens(value) {
    return value
      .split(",")
      .map(function (part) {
        return part.trim();
      })
      .filter(Boolean);
  }

  function currentTokenBounds(value) {
    var comma = value.lastIndexOf(",");
    if (comma === -1) {
      var start = 0;
      while (start < value.length && value.charAt(start) === " ") {
        start += 1;
      }
      return { prefix: "", token: value.slice(start) };
    }
    var after = comma + 1;
    while (after < value.length && value.charAt(after) === " ") {
      after += 1;
    }
    return {
      prefix: value.slice(0, after),
      token: value.slice(after),
    };
  }

  function optionValues(options, value) {
    var bounds = currentTokenBounds(value);
    var selected = {};
    splitTokens(value).forEach(function (token) {
      selected[token] = true;
    });
    var token = bounds.token;
    var tokenLower = token.toLowerCase();
    var prefix = bounds.prefix;
    var result = [];
    options.forEach(function (option) {
      if (selected[option] && option !== token) {
        return;
      }
      if (token && option.toLowerCase().indexOf(tokenLower) === -1) {
        return;
      }
      result.push(prefix + option);
    });
    return result;
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
      var values = optionValues(options, input.value);
      while (datalist.firstChild) {
        datalist.removeChild(datalist.firstChild);
      }
      values.forEach(function (value) {
        var node = document.createElement("option");
        node.value = value;
        datalist.appendChild(node);
      });
    }

    input.addEventListener("input", refresh);
    input.addEventListener("focus", refresh);
  }

  function enhance(rootNode) {
    var scope = rootNode || document;
    scope.querySelectorAll("input[data-token-complete]").forEach(bindInput);
  }

  if (typeof document !== "undefined") {
    enhance(document);
  }

  root.filterComplete = {
    splitTokens: splitTokens,
    currentTokenBounds: currentTokenBounds,
    optionValues: optionValues,
    enhance: enhance,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
