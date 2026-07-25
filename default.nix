{ pkgs ? import <nixpkgs> { } }:

let
  inherit (pkgs) lib python3Packages;
in

python3Packages.buildPythonApplication {
  pname = "hermes-tracker";
  version = "0.1.0";
  pyproject = true;

  src = ./.;

  build-system = with python3Packages; [ hatchling ];
  dependencies = with python3Packages; [
    bleach
    fastapi
    jinja2
    mistune
    pydantic
    uvicorn
  ];

  # nixpkgs may carry newer compatible releases than the conservative
  # upper bounds used for lock-file stability.
  pythonRelaxDeps = [ "fastapi" "uvicorn" ];

  nativeCheckInputs = with python3Packages; [
    httpx
    pytestCheckHook
  ];

  pythonImportsCheck = [ "hermes_tracker" ];

  meta = {
    description = "Small agent-only TODO tracker with a public read-only frontend";
    homepage = "https://todos.thanosapollo.org";
    license = lib.licenses.mit;
    mainProgram = "uvicorn";
  };
}
