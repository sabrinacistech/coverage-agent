# External Client Map

## Objetivo
Identificar clientes externos (HTTP, gRPC, mensajería, BD no-JPA) usados por el SUT y sus excepciones, para generar tests de error reproducibles sin red.

## Procedimiento
1. Heurísticas + verificación AST:
   - Clientes Spring: `RestTemplate`, `WebClient`, `RestClient`.
   - Feign: interfaces con `@FeignClient`.
   - gRPC stubs generados (`*BlockingStub`, `*Stub`).
   - JMS/Kafka: `JmsTemplate`, `KafkaTemplate`, `Producer`, `Consumer`.
   - JDBC: `JdbcTemplate`, `NamedParameterJdbcTemplate`.
2. Por cada cliente registrar:
   - método invocado, firma, tipo de retorno,
   - excepciones declaradas y de runtime esperables (`RestClientException`, `FeignException`, `JmsException`, etc.).

## Salida
Bloque `externalClients[]` en `dependency-graph.json`, con `evidenceId` por método.

## Reglas
- Prohibido tests que abran sockets, conexiones JDBC reales o brokers; todo se mockea.
- Para errores: usar la excepción declarada en el cliente, no genérica `Exception`.
