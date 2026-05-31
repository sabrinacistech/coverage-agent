# FreeBuilder Policy

## Problema

En proyectos con FreeBuilder es frecuente que el LLM genere errores como:

- `new Interface()` sobre interfaces.
- Uso directo de clases generadas `Interface_Builder`.
- Setters inventados.
- Builders inexistentes.

## Política

1. Si el tipo es interface con `@FreeBuilder`, no instanciar con `new`.
2. Usar `new Type.Builder()` solo si existe una clase `Builder` declarada en el source.
3. No usar `Type_Builder` salvo que el contrato lo permita explícitamente.
4. Si no hay builder confirmado, usar `Mockito.mock(Type.class)` para objetos pasivos.
5. Si el objeto participa en lógica de negocio, crear fixture solo con builder/factory/constructor verificado.
