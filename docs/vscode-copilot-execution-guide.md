# Guía de ejecución desde Visual Studio Code con GitHub Copilot

## Objetivo

Evitar que Copilot genere tests que compilan en apariencia pero fallan por imports, interfaces instanciadas, builders FreeBuilder inexistentes o métodos inventados.

## Regla operativa principal

Copilot solo puede generar código usando los archivos `state/*.json` producidos por el pre-stage determinístico. Si un símbolo no está en `state/import-whitelist.json`, `state/symbol-contracts/` o `state/fixture-catalog.json`, no debe usarse.

## Flujo recomendado en VS Code

1. Abrir la raíz real del repositorio Java, no solo una carpeta parcial.
2. Ejecutar el build de clases antes de pedir tests:

```bash
mvn -q -DskipTests package
```

3. Ejecutar el pre-stage de la arquitectura:

```bash
python docs/agents/java-test-coverage-architecture/tools/python/run_pipeline.py \
  --repo . \
  --out docs/agents/java-test-coverage-architecture/state \
  --module <modulo> \
  --include-fqcn '^ar\.com\.' \
  --jacoco-xml <modulo>/target/site/jacoco/jacoco.xml
```

4. Pedir a Copilot que trabaje solo sobre un SUT y un test por vez.
5. Antes de aceptar cambios, ejecutar el pre-compile linter:

```bash
python docs/agents/java-test-coverage-architecture/tools/python/test_linter.py \
  --test-file <ruta-del-test-generado>.java \
  --whitelist docs/agents/java-test-coverage-architecture/state/import-whitelist.json \
  --contracts docs/agents/java-test-coverage-architecture/state/symbol-contracts \
  --stack-profile docs/agents/java-test-coverage-architecture/state/stack-profile.json
```

> **NOTA:** `--stack-profile` es requerido por la arquitectura para validación completa de G5
> (frameworks disponibles según el stack real del proyecto).
> Si la versión local de `test_linter.py` aún no soporta este flag, ejecutar sin él
> temporalmente hasta completar la mejora 9 (integración de stack-profile en el linter):
> ```bash
> python docs/agents/java-test-coverage-architecture/tools/python/test_linter.py \
>   --test-file <ruta-del-test-generado>.java \
>   --whitelist docs/agents/java-test-coverage-architecture/state/import-whitelist.json \
>   --contracts docs/agents/java-test-coverage-architecture/state/symbol-contracts
> ```

6. Recién después ejecutar Maven en scope angosto:

```bash
mvn -pl <modulo> -am -Dtest=<NombreTest> -DfailIfNoTests=false test
```

## Prompt recomendado para Copilot Chat

```text
Actúa como Generation Agent de la arquitectura java-test-coverage-architecture.

Restricciones obligatorias:
- No inventes imports, clases, constructores, métodos ni builders.
- Usa únicamente símbolos presentes en state/import-whitelist.json y state/symbol-contracts/*.json.
- Si el tipo es interface, abstract o @FreeBuilder, no uses new Tipo().
- Para FreeBuilder usa new Tipo.Builder() solo si el contrato contiene un builder con entry new Tipo.Builder().
- No uses Tipo_Builder salvo que el contrato lo permita explícitamente.
- No agregues setters/métodos que no estén enumerados en builders[].setters[] o methods[].
- Si falta evidencia, descarta el caso de test y explica el motivo.
- Genera un solo archivo de test y no modifiques POM ni código productivo.

Tarea:
Generar o corregir tests unitarios para <FQCN_SUT>, usando <ruta contrato JSON> y <ruta fixture catalog>.
Antes de entregar, validar mentalmente G1, G2 y G6.
```

## Diagnósticos típicos y corrección esperada

| Error | Causa raíz | Corrección válida |
|-------|------------|-------------------|
| `The import X cannot be resolved` | Copilot importó una clase que no existe en el classpath del módulo | quitar import/uso; solo reemplazar si hay match único en whitelist |
| `Cannot instantiate the type NaturalPerson` | `NaturalPerson` es interface, abstract o FreeBuilder | usar builder verificado o `mock(NaturalPerson.class)` si es pasivo |
| `new NaturalPerson()` | alucinación de constructor | bloqueado por G2; usar `instantiation.strategy` del contrato |
| `The method setX(...) is undefined for the type Y` | setter/método inventado | usar solo `methods[]` o `builders[].setters[]` del contrato |
| `Type_Builder cannot be resolved` | uso directo de clase generada no expuesta | usar `new Type.Builder()` solo si existe en contrato |

## Configuración sugerida del workspace

Agregar `.github/copilot-instructions.md` y usar el prompt anterior como regla de proyecto. En VS Code, mantener habilitado Java Language Server para ver diagnósticos JDT antes de compilar.
