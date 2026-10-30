#!/bin/sh

echo "========================================="
echo "   Telegram Download Daemon Plus - BUILD "
echo "========================================="
echo ""
echo "Selecciona el tipo de operacion:"
echo "1) ? Recrear rapido (Aplica cambios del docker-compose.yml)"
echo "   - Ideal para cambios de variables de entorno (como el TZ)"
echo "   - No recompila la imagen, ahorra tiempo"
echo ""
echo "2) ?? Rebuild Completo (sin cache)"
echo "   - Para cambios en dependencias o codigo del Dockerfile"
echo "   - Descarga todo desde cero (mas lento)"
echo ""
read -p "Opcion [1/2]: " option

echo ""

case $option in
    1)
        echo "? Recreando contenedor rapidamente con los nuevos cambios..."
        docker-compose up -d --force-recreate
        ;;
    2)
        echo "?? Parando contenedor para rebuild profundo..."
        docker-compose down
        echo "?? Iniciando construccion COMPLETA (sin cache)..."
        docker-compose build --no-cache
        echo "?? Levantando nuevo contenedor..."
        docker-compose up -d
        ;;
    *)
        echo "?? Opcion no valida. Aplicando recreacion rapida por defecto."
        docker-compose up -d --force-recreate
        ;;
esac

echo ""
echo "? Proceso completado"