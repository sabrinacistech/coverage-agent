# Java 8 Compatibility

Aplicar solo si `stack-profile.java == "1.8"`.

## Permitido
- Lambdas, method references, `Optional`, `Stream`, `java.time`.
- `default` methods en interfaces.

## Prohibido
- `var` (Java 10+).
- `record`, `sealed`, pattern matching, text blocks (Java 14+).
- `List.of`, `Map.of`, `Set.of` (Java 9+) ⇒ usar `Collections.singletonList`, `Arrays.asList`, `new HashMap<>()`.
- `Optional.isEmpty()` (Java 11+) ⇒ usar `!opt.isPresent()`.
- `Stream.toList()` (Java 16+) ⇒ `.collect(Collectors.toList())`.
- `String.isBlank`, `String.strip`, `String.lines`, `String.repeat` (Java 11+).
- `Files.readString`, `Files.writeString` (Java 11+).

## Reglas adicionales
- AssertJ y Mockito deben ser versiones compatibles con Java 8 (verificadas en stack-profile).
- Si el módulo tiene `maven.compiler.release=8` pero el código productivo usa API >8, registrar inconsistencia y abortar (no es válido tu profile).
