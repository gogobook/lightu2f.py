__all__ = [
    'generate_enrollment_request',
    'generate_idassertion_request',
    'process_enrollment_response',
    'process_idassertion_response',
]


import base64
import json


def generate_enrollment_request(app_id, transaction_nonce, khs):
    return JSON_encode({
        'type': 'u2f_register_request',
        'registerRequests': [
            {
                'appId': app_id,
                'challenge': WS64_encode(transaction_nonce),
                'version': 'U2F_V2'
            }
        ],
        'signRequests': [
            {
                'appId': app_id,
                'challenge': '',
                'keyHandle': WS64_encode(kh),
                'version': 'U2F_V2'
            } for kh in khs
        ]
    })


def generate_idassertion_request(app_id, transaction_nonce, kh_pk_cnts):
    return JSON_encode({
        'type': 'u2f_sign_request',
        'signRequests': [
            {
                'appId': app_id,
                'challenge': WS64_encode(b''.join([
                    transaction_nonce, pk, (
                        b'\x00' * 5 if cnt is None else
                        b'\x01' + cnt.to_bytes(4, 'big')
                    )
                ])),
                'keyHandle': WS64_encode(kh),
                'version': 'U2F_V2'
            } for kh, pk, cnt in kh_pk_cnts
        ]
    })


def process_enrollment_response(app_id, transaction_nonce, enr_response):
    try:
        enr_response_dct = JSON_decode(enr_response)
        assert type(enr_response_dct) is dict
        assert enr_response_dct.keys() >= {'clientData', 'registrationData'}
        assert type(enr_response_dct['clientData']) is str
        assert type(enr_response_dct['registrationData']) is str

        client_data_b64 = enr_response_dct['clientData']
        registration_response_b64 = enr_response_dct['registrationData']

        client_data_raw = WS64_decode(client_data_b64)
        client_data_str = UTF8_decode(client_data_raw)
        client_data_dct = JSON_decode(client_data_str)
        assert type(client_data_dct) is dict
        assert client_data_dct.keys() >= {'typ', 'challenge', 'origin'}
        assert type(client_data_dct['typ']) is str
        assert type(client_data_dct['challenge']) is str
        assert type(client_data_dct['origin']) is str
        assert client_data_dct['typ'] == 'navigator.id.finishEnrollment'
        assert WS64_decode(client_data_dct['challenge']) == transaction_nonce
        facetid = client_data_dct['origin']
        cidinfo = (
            JSON_encode(client_data_dct['cid_pubkey'])
            if 'cid_pubkey' in client_data_dct else None
        )

        registration_response_raw = WS64_decode(registration_response_b64)
        assert len(registration_response_raw) >= 67
        assert registration_response_raw[0] == 0x05
        publickey = registration_response_raw[1:66]
        deserialize_public_key_in_uncompressed_format(publickey)
        L = registration_response_raw[66]
        assert len(registration_response_raw) >= 67 + L
        keyhandle = registration_response_raw[67:67+L]
        certificate, signature_to_verify = extract_one_DER_encoded_value(
            registration_response_raw[67+L:]
        )
        attest_pubkey_Q = x509decode_p256ecdsa_publickey(certificate)

        data_to_sign = b''.join([
            b'\x00',
            sha256(UTF8_encode(app_id)),  # application parameter
            sha256(client_data_raw),  # challenge parameter
            keyhandle,
            publickey,
        ])
        assert is_good_signature(attest_pubkey_Q, data_to_sign, signature_to_verify)

        return facetid, keyhandle, publickey, certificate, cidinfo

    except AssertionError:
        raise ValueError from None


def process_idassertion_response(app_id, transaction_nonce, ida_response):
    try:
        ida_response_dct = JSON_decode(ida_response)
        assert type(ida_response_dct) is dict
        assert ida_response_dct.keys() >= {'keyHandle', 'clientData', 'signatureData'}
        assert type(ida_response_dct['keyHandle']) is str
        assert type(ida_response_dct['clientData']) is str
        assert type(ida_response_dct['signatureData']) is str

        keyhandle_b64 = ida_response_dct['keyHandle']
        client_data_b64 = ida_response_dct['clientData']
        authentication_response_b64 = ida_response_dct['signatureData']

        claimed_keyhandle = WS64_decode(keyhandle_b64)

        # Now we have:
        #   claimed_keyhandle

        client_data_raw = WS64_decode(client_data_b64)
        client_data_str = UTF8_decode(client_data_raw)
        client_data_dct = JSON_decode(client_data_str)
        assert type(client_data_dct) is dict
        assert client_data_dct.keys() >= {'typ', 'challenge', 'origin'}
        assert type(client_data_dct['typ']) is str
        assert type(client_data_dct['challenge']) is str
        assert type(client_data_dct['origin']) is str
        assert client_data_dct['typ'] == 'navigator.id.getAssertion'
        facetid = client_data_dct['origin']
        client_data_challenge = WS64_decode(client_data_dct['challenge'])
        L = len(transaction_nonce)
        assert len(client_data_challenge) == L + 65 + 1 + 4
        assert client_data_challenge.startswith(transaction_nonce)
        claimed_publickey = client_data_challenge[L:L+65]
        assert client_data_challenge[L+65] in {0, 1}
        if client_data_challenge[L+65] == 1:
            claimed_old_counter = int.from_bytes(client_data_challenge[-4:], 'big')
        else:
            claimed_old_counter = None
        cidinfo = (
            JSON_encode(client_data_dct['cid_pubkey'])
            if 'cid_pubkey' in client_data_dct else None
        )

        # Now we have:
        #   facetid
        #   client_data_raw
        #   claimed_publickey
        #   claimed_old_counter

        authentication_response_raw = WS64_decode(authentication_response_b64)
        assert len(authentication_response_raw) >= 5
        assert authentication_response_raw[0] == 1
        claimed_new_counter_raw = authentication_response_raw[1:5]
        claimed_new_counter = int.from_bytes(claimed_new_counter_raw, 'big')
        signature_to_verify = authentication_response_raw[5:]

        # Now we have:
        #   claimed_new_counter
        #   signature_to_verify

        data_to_sign = b''.join([
            sha256(UTF8_encode(app_id)),  # application parameter
            b'\x01',
            claimed_new_counter_raw,
            sha256(client_data_raw),  # challenge parameter
        ])
        assert is_good_signature(claimed_publickey, data_to_sign, signature_to_verify)

        return (
            facetid,
            claimed_keyhandle,
            claimed_publickey,
            claimed_old_counter,
            claimed_new_counter,
            cidinfo
        )

    except AssertionError:
        raise ValueError from None


def WS64_encode(raw):  # bytes -> str
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def WS64_decode(txt):  # str -> bytes ^ ValueError
    try:
        result = base64.urlsafe_b64decode(txt + '==')
        assert WS64_encode(result) == txt
        return result
    except AssertionError:
        raise ValueError from None
    except base64.binascii.Error:
        raise ValueError from None


def UTF8_encode(s):  # str -> bytes
    # NOTE we _MAY_ choose to restrict the input to only ASCII printable characters
    return s.encode()


def UTF8_decode(x):  # bytes -> str ^ ValueError
    # NOTE we _MAY_ choose to restrict the input to only ASCII printable characters
    try:
        return x.decode()
    except UnicodeDecodeError:
        raise ValueError from None


def JSON_encode(val):  # ? -> str
    return json.dumps(val, separators=(',', ':'), sort_keys=True)


def JSON_decode(txt):  # str -> ? ^ ValueError
    try:
        return json.loads(txt)
    except json.decoder.JSONDecodeError:
        raise ValueError from None




##############################################################################
#                                                                            #
#   ECDSA signature with SHA-256 verification routines on secp256r1 curve    #
#                                                                            #
##############################################################################


from hashlib import sha256 as _sha256


_n = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551


def is_good_signature(publickey, message, signature):
    if type(publickey) is bytes:
        Q = deserialize_public_key_in_uncompressed_format(publickey)
    else:
        Q = publickey
    try:
        _r, _s = DER_decode_one_SEQUENCE(signature)
        r = DER_decode_one_INTEGER(_r)
        s = DER_decode_one_INTEGER(_s)
    except ValueError:
        return False
    if not (0 < r < _n and 0 < s < _n):
        return False
    e = _hash_to_finite_field_n_using_sha256(message)
    si = _inv_mod_n(s)
    t, u = e * si % _n, r * si % _n
    return (double_scalarmul(t, u, Q) - r) % _n == 0


def sha256(msg):
    return _sha256(msg).digest()


def _hash_to_finite_field_n_using_sha256(msg):
    return int.from_bytes(sha256(msg), 'big')  # % _n


def _inv_mod_n(a):
    s, t, x2, x1, = a, _n, 1, 0
    while t > 0:
        q, r = divmod(s, t)
        x = x2 - q * x1
        s, t, x2, x1 = t, r, x1, x
    return x2




##############################################################################
#                                                                            #
#   Some secp256r1 curve routines that involve GF(p) arithmetics             #
#                                                                            #
##############################################################################


_p = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
_4b = 0x6b18d763a8ea4f9dcfaef555da621af194741ac2314ec3d8ef38f0f89f49812d
_b = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
_G = (
    0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
    0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5
)


#
# {0, ..., 255}* -> E\{O} + {"INVALID"}
#
def deserialize_public_key_in_uncompressed_format(QQ):
    if len(QQ) == 65 and QQ[0] == 0x04:
        x = int.from_bytes(QQ[1:33], 'big')
        y = int.from_bytes(QQ[33:65], 'big')
        if x < _p and y < _p and (
            x * ((x ** 2 - 3) % _p) + _b - y ** 2
        ) % _p == 0:
            return x, y
    raise ValueError


#
# {0, ..., 255}* -> E\{O} + {"INVALID"}
#
def deserialize_public_key_in_compressed_format(QQ):
    if len(QQ) == 33 and QQ[0] in [0x02, 0x03]:
        x = int.from_bytes(QQ[1:33], 'big')
        y_squared = (x * ((x ** 2 - 3) % _p) + _b) % _p
        # _e = (_p + 1) // 4
        _e = 0x3fffffffc0000000400000000000000000000000400000000000000000000000
        y = pow(a, _e, _p)
        if x < _p and y ** 2 % _p == y_squared:
            return x, (y if QQ[0] % 2 == y % 2 else _p - y)
    raise ValueError


#
# {0, ..., 255}* -> E\{O} + {"INVALID"}
#
def deserialize_public_key_in_any_format(QQ):
    if len(QQ) == 65:
        return deserialize_public_key_in_uncompressed_format(QQ)
    elif len(QQ) == 33:
        return deserialize_public_key_in_compressed_format(QQ)
    raise ValueError


#
# [0, n-1] x [1, n-1] x E\{O} -> [0, p-1]
#
# t, u, Q  |->
#   if [t]G [+] [u]Q == O then 0
#   if [t]G [+] [u]Q != O then _x_of_([t]G [+] [u]Q)
#
def double_scalarmul(t, u, Q):
    T = _mul(t, _G)
    U = _mul(u, Q)
    return _add_then_zero_if_infinity_else_x(T, U)


#
# E x E\{O} -> [0, p-1]
#
# T, U  |->
#   if T [+] U == O then 0
#   if T [+] U != O then _x_of_(T [+] U)
#
# Note that T == O iff t == 0 iff e == 0 which could hardly happen even if you
# want to force it.  The probability of T [+] U == O should be low too.
#
def _add_then_zero_if_infinity_else_x(T, U):

    # T is O
    if T is None:
        return U[0]

    # T and U are different and not negative to each other
    elif T[0] != U[0]:
        x1, y1 = T
        x2, y2 = U
        v = ((y2 - y1) * _inv_mod_p(x2 - x1)) % _p
        x3 = (v * v - x1 - x2) % _p
        return x3

    # T and U is the same point
    elif (T[1] - U[1]) % _p == 0:
        x1, y1 = T
        w = (((3 * x1 * x1 - 3) % _p) * _inv_mod_p(2 * y1)) % _p
        x4 = (w * w - x1 - x1) % _p
        return x4

    # T and U are different and negative to each other
    else:
        return 0


#
# [1, p-1] -> [1, p-1]
#
# a  |->  1/a mod p  (the result is not guaranteed to be already reduced)
#
# this function is invoked 1 + 1 = 2 times when generating a signature
# this function is invoked 1 + 3 = 4 times when verifying a signature
#
def _inv_mod_p(a):
    s, t, x2, x1, = a, _p, 1, 0
    while t > 0:
        q, r = divmod(s, t)
        x = x2 - q * x1
        s, t, x2, x1 = t, r, x1, x
    return x2


#
# [0, n-1] x E\{O} -> E
#
# k, P  |->  [k]P
#
# It is _REQUIRED_ that the x-coordinate of the returned point is already
# reduced modulo p.  The y coordinate could be any equivalent value modulo p.
#
def _mul(k, P):
    # n1 = n-1
    n1 = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632550
    if k != 0 and k != n1:
        return _mul_using_montlad(k, P)
    elif k != 0:
        return P[0], -P[1]  # % _p
    else:
        return None


#
# [1, n-2] x E\{O} -> E\{O}
#
# k, P  |->  [k]P
#
# It is _REQUIRED_ that the x-coordinate of the returned point is already
# reduced modulo p.  The y coordinate could be any equivalent value modulo p.
#
# Every time when we verify a signature, there is >99% possibility that this
# function is invoked twice.
#
def _mul_using_montlad(k, P):

    # P  |->  ([1]P, [2]P)
    #
    # We use a 7-tuple (X1, X2, TD, Ta, Tb, x, y) to
    # represent a differential pair ([j]P, [j+1]P)
    #
    #   X1 = (the x-coordinate of [j]P) * Z
    #   X2 = (the x-coordinate of [j+1]P) * Z
    #   TD = xD * Z
    #   Ta = a * Z * Z
    #   Tb = 4 * b * Z * Z * Z
    #   x = (the x-coordinate of P)
    #   y = (the y-coordinate of P)
    #
    x, y = P
    t1 = 2 * y
    t2 = (3 * (x ** 2) - 3) % _p
    Z = (t1 ** 2) % _p
    ZZ = (Z ** 2) % _p
    ZZZ = (ZZ * Z) % _p
    X1 = (x * Z) % _p
    X2 = (t2 ** 2 - 2 * x * Z) % _p
    Ta = -3 * ZZ
    Tb = (_4b * ZZZ) % _p
    PP = [X1, X2, X1, Ta, Tb, x, y]

    # scan all bits from left to right, discarding the first one
    for bit in bin(k)[3:]:

        if bit == '0':
            # ([j]P, [j+1]P)  |->  ([2j]P, [2j+1]P)
            X2, X1, TD, Ta, Tb = PP[0:5]
            R2 = (X1 - X2)
            R1 = (R2 * R2) % _p
            R2 = (X2 * X2) % _p
            R3 = (R2 - Ta)
            R4 = (R3 * R3) % _p
            R5 = (X2 + X2)
            R3 = (R5 * Tb) % _p
            R4 = (R4 - R3)
            R5 = (R5 + R5)
            R2 = (R2 + Ta)
            R3 = (R5 * R2) % _p
            R3 = (R3 + Tb)
            R5 = (X1 + X2)
            R2 = (R2 + Ta)
            R2 = (R2 - R1)
            X2 = (X1 * X1) % _p
            R2 = (R2 + X2)
            X2 = (R5 * R2) % _p
            X2 = (X2 + Tb)
            X1 = (R3 * X2) % _p
            X2 = (R1 * R4) % _p
            R2 = (R1 * R3) % _p
            R3 = (R2 * Tb) % _p
            R4 = (R2 * R2) % _p
            R1 = (TD * R2) % _p
            R2 = (Ta * R4) % _p
            Tb = (R3 * R4) % _p
            X1 = (X1 - R1)
            TD = R1
            Ta = R2
            PP[0:5] = X2, X1, TD, Ta, Tb

        else:
            # ([j]P, [j+1]P)  |->  ([2j+1]P, [2j+2]P)
            X1, X2, TD, Ta, Tb = PP[0:5]
            R2 = (X1 - X2)
            R1 = (R2 * R2) % _p
            R2 = (X2 * X2) % _p
            R3 = (R2 - Ta)
            R4 = (R3 * R3) % _p
            R5 = (X2 + X2)
            R3 = (R5 * Tb) % _p
            R4 = (R4 - R3)
            R5 = (R5 + R5)
            R2 = (R2 + Ta)
            R3 = (R5 * R2) % _p
            R3 = (R3 + Tb)
            R5 = (X1 + X2)
            R2 = (R2 + Ta)
            R2 = (R2 - R1)
            X2 = (X1 * X1) % _p
            R2 = (R2 + X2)
            X2 = (R5 * R2) % _p
            X2 = (X2 + Tb)
            X1 = (R3 * X2) % _p
            X2 = (R1 * R4) % _p
            R2 = (R1 * R3) % _p
            R3 = (R2 * Tb) % _p
            R4 = (R2 * R2) % _p
            R1 = (TD * R2) % _p
            R2 = (Ta * R4) % _p
            Tb = (R3 * R4) % _p
            X1 = (X1 - R1)
            TD = R1
            Ta = R2
            PP[0:5] = X1, X2, TD, Ta, Tb

    # ([j]P, [j+1]P)  |->  [j]P
    X1, X2, TD, Ta, Tb, xD, yD = PP
    R1 = (TD * X1) % _p
    R2 = (R1 + Ta)
    R3 = (X1 + TD)
    R4 = (R2 * R3) % _p
    R3 = (X1 - TD)
    R2 = (R3 * R3) % _p
    R3 = (R2 * X2) % _p
    R4 = (R4 - R3)
    R4 = (R4 + R4)
    R4 = (R4 + Tb)
    R2 = (TD * TD) % _p
    R3 = (X1 * R2) % _p
    R1 = (xD * R3) % _p
    R3 = (yD + yD)
    R3 = (R3 + R3)
    X1 = (R3 * R1) % _p
    R1 = (R2 * TD) % _p
    Z_ = (R3 * R1) % _p
    R2 = (xD * xD) % _p
    R3 = (R2 * xD) % _p
    X2 = (R3 * R4) % _p
    Zi = _inv_mod_p(Z_)
    xQ = (X1 * Zi) % _p
    yQ = (X2 * Zi)  # % _p
    return xQ, yQ




##############################################################################
#                                                                            #
#   X.609 DER decoding routines for commonly used data structures            #
#                                                                            #
##############################################################################


def extract_one_DER_encoded_value(octets):
    """Extract the leading DER encoded value.

    Return a tuple of two octet strings where the first one is
    the extracted DER encoded value and the second one contains
    all the subsequent uninterpreted octets.

    Raise ValueError if the provided octet string is ill-formed.
    """
    T, L, V, Z = DER_decode_one_something(octets)
    return T + L + V, Z


def x509decode_p256ecdsa_publickey(certificate):
    """Extract subject public key from the provided certificate.

    Raise ValueError if the provided certificate is ill-formed.
    """
    tbscert, _, _ = DER_decode_one_SEQUENCE(certificate)
    _, _, _, _, _, _, pkinfo, *_ = DER_decode_one_SEQUENCE(tbscert)
    alg, pkbits = DER_decode_one_SEQUENCE(pkinfo)
    P256PUBKEY = bytes.fromhex('301306072a8648ce3d020106082a8648ce3d030107')
    if not (alg == P256PUBKEY and pkbits[:3] == b'\x03\x42\x00'):
        raise ValueError
    Q = deserialize_public_key_in_any_format(pkbits[3:])
    return Q


def DER_decode_one_INTEGER(octets):
    T, L, V, tail = DER_decode_one_something(octets)
    if not (T == b'\x02' and tail == b''):
        raise ValueError
    if len(V) == 0:
        raise ValueError
    if len(V) >= 2 and (
        (V[0] == 0b00000000 and V[1] >> 7 == 0) or
        (V[0] == 0b11111111 and V[1] >> 7 == 1)
    ):
        raise ValueError
    return int.from_bytes(V, 'big', signed=True)


def DER_decode_one_SEQUENCE(octets):
    T, L, V, tail = DER_decode_one_something(octets)
    if not (T == b'\x30' and tail == b''):
        raise ValueError
    elms, tail = [], V
    while tail != b'':
        T, L, V, tail = DER_decode_one_something(tail)
        elms.append(T + L + V)
    return tuple(elms)


def DER_decode_one_something(octets):
    T, tail1 = DER_extract_identifier_octets(octets)
    L, tail2 = DER_extract_length_octets(tail1)
    V_length = DER_decode_length_octets(L)
    V, tail3 = tail2[:V_length], tail2[V_length:]
    return T, L, V, tail3


def DER_extract_identifier_octets(stream):
    try:
        assert len(stream) >= 1
        if stream[0] & 0b00011111 != 0b00011111:
            return stream[:1], stream[1:]
        else:
            assert len(stream) >= 2
            l = next(i for i, e in enumerate(stream[1:]) if e >> 7 == 0)
            if l == 0:
                assert stream[1] >= 0b00011111
            else:
                assert stream[1] & 0b01111111 != 0
            return stream[:l+2], stream[l+2:]
    except AssertionError:
        raise ValueError from None
    except StopIteration:
        raise ValueError from None


def DER_extract_length_octets(stream):
    try:
        assert len(stream) >= 1
        if stream[0] >> 7 == 0:
            return stream[:1], stream[1:]
        else:
            l = stream[0] & 0b01111111
            assert 1 <= l <= 126
            assert len(stream) >= l + 1
            assert (l == 1 and stream[1] >= 128) or (l > 1 and stream[1] != 0)
            return stream[:l+1], stream[l+1:]
    except AssertionError:
        raise ValueError from None


def DER_decode_length_octets(length_octets):
    if length_octets[0] < 128:
        return length_octets[0]
    else:
        return int.from_bytes(length_octets[1:], 'big')
