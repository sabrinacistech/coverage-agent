# Static Architecture Review Rules

- Controller no debería acceder directamente a Repository.
- Entity no debería exponerse como contrato público de API.
- Service debería concentrar casos de uso.
- Configuración sensible no debe estar en application.yml/properties.
- Observabilidad debería tener health checks, métricas y logging consistente.
