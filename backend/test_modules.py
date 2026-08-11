import asyncio

# Test validation
from app.services.validation import ValidationService, FuzzyService

fields = {
    'last_name': {'value': 'DUPONT', 'confidence': 0.95, 'alerts': []},
    'birth_date': {'value': '1985-03-15', 'confidence': 0.95, 'alerts': []},
    'id_number': {'value': '051234567890', 'confidence': 0.95, 'alerts': []},
}
template = {'fields': [
    {'id': 'last_name', 'type': 'string', 'validation': {'required': True}},
    {'id': 'birth_date', 'type': 'date', 'validation': {'required': True}},
    {'id': 'id_number', 'type': 'string', 'validation': {'required': True}},
]}

svc = ValidationService()
result = asyncio.run(svc.validate(fields, template))
print('VALIDATION OK:', result)

# Test fuzzy
fuzzy_svc = FuzzyService()
fuzz_result = asyncio.run(fuzzy_svc.match(
    fields,
    {'last_name': 'DUPONT', 'birth_date': '1985-03-15'},
    template
))
print('FUZZY OK:', fuzz_result)

# Test biometric init
from app.services.biometric import BiometricService
bio = BiometricService()
print('BIOMETRIC INIT OK, simulation_mode=', bio.simulation_mode)
