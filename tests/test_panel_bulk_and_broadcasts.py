"""Тесты маршрутов /panel/staff-bulk и /panel/broadcasts."""
import pytest
import repository as repo
from conftest import login_panel, post_form, register, add_staff

pytestmark = pytest.mark.panel


async def test_staff_bulk_route_get_and_post(panel_client):
    """Маршрут /panel/staff-bulk доступен, отображает форму и обрабатывает запросы."""
    assert login_panel(panel_client)
    res = panel_client.get('/panel/staff-bulk')
    assert res.status_code == 200
    assert 'Выпуск сотрудников по ссылкам' in res.text
    assert 'Кого заводим' in res.text
    assert 'action="/panel/staff-bulk"' in res.text

    # Проверка POST /panel/staff-bulk
    post_res = post_form(panel_client, '/panel/staff-bulk', {
        'rows': 'Смирнов Алексей Петрович; Секретарь; 101; Справки',
        'action': 'check',
    })
    assert post_res.status_code == 200
    assert 'Смирнов Алексей Петрович' in post_res.text
    assert 'Проверено строк' in post_res.text


async def test_broadcasts_with_populated_contacts(panel_client):
    """Маршрут /panel/broadcasts не падает с AttributeError при наличии контактов в базе."""
    await register('201', 'Иванова Ольга Сергеевна', 'ИС-21')
    await add_staff('202', 'Петров Денис Николаевич', position='Преподаватель')
    await repo.touch_contact('202', 'petrov', 'Денис', 'Здравствуйте')
    await repo.touch_contact('203', 'guest_user', 'Гость Антон', 'Привет')
    assert login_panel(panel_client)

    res = panel_client.get('/panel/broadcasts')
    assert res.status_code == 200
    assert 'Новая рассылка' in res.text
    assert 'recipients-list' in res.text
    assert '201' in res.text
    assert '202' in res.text
    assert '203' in res.text
    assert 'Иванова Ольга Сергеевна' in res.text
    assert 'Петров Денис Николаевич' in res.text
